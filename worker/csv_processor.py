"""
CSVForge worker — CSV parsing, validation, cleaning, and writing.

WHAT:
    This module provides three pure-Python functions for processing CSV files:
      1. parse_and_analyse(file_path) → AnalysisResult
         Reads a CSV file, validates it against the AD-21 contract, and returns
         column metadata + all parsed rows as list-of-dicts.
      2. clean(rows) → CleanResult
         Trims whitespace from all string values, then deduplicates rows.
         Returns the cleaned rows and the count of duplicates removed.
      3. write_csv(file_path, column_names, rows) → None
         Writes a list of row dicts to a CSV file using csv.DictWriter.

WHY:
    Separating CSV logic from the Lambda handler (handler.py) makes each unit
    independently testable. The handler orchestrates the workflow; this module
    handles the file processing mechanics. Neither module needs to know about
    the other's AWS wiring.

    Pure Python (no AWS SDK calls) makes the functions easy to unit-test:
    create a temp file, call parse_and_analyse(), assert on the result — no
    mocking of S3, SQS, or PostgreSQL needed.

ARCHITECTURE:

    Lambda handler (handler.py)
          │
          │ 1. download_file(bucket, key, /tmp/input.csv)
          ▼
    parse_and_analyse("/tmp/input.csv")
          │
          ├── InvalidCSVError raised? → handler catches, marks job failed
          │
          └── AnalysisResult returned
                    │
                    ▼
             clean(result.rows)
                    │
                    └── CleanResult returned
                              │
                              ▼
                   write_csv("/tmp/output.csv", ...)
                              │
                              ▼
                   upload_file(bucket, output_key, "/tmp/output.csv")

VALIDATION CONTRACT (AD-21):
    VALID inputs processed without error:
      - UTF-8 encoded CSV
      - UTF-8 with BOM (utf-8-sig) — BOM stripped transparently
      - CRLF, LF, or mixed line endings — csv.reader handles these natively
      - Quoted fields (quoted commas, quoted newlines)
      - Header-only CSV with zero data rows (row_count=0)

    INVALID inputs — raise InvalidCSVError (permanent failure, do not retry):
      - Empty file (0 bytes)
      - Invalid UTF-8 encoding (not decodable as UTF-8)
      - Duplicate column names
      - Empty header name (column with no name)
      - Malformed CSV quoting (e.g., unclosed quote)
      - Rows with more or fewer fields than headers
      - Entirely binary content (non-decodable as text)

CLEANING CONTRACT:
    The clean() function enforces this exact order — order matters:
      1. TRIM: strip leading/trailing whitespace from every string value.
      2. DEDUP: remove rows that are identical after trimming.
    Order matters because two rows that appear distinct before trimming
    (e.g., "Alice " and "Alice") may become duplicates after trimming.
    If you deduplicated before trimming, you would keep both. If you trim
    before deduplicating, you correctly identify them as duplicates.

IMPORTANT CONCEPTS:
    AnalysisResult dataclass:
        A lightweight data container returned by parse_and_analyse().
        Using a dataclass (rather than a dict or tuple) makes the return type
        self-documenting and gives the caller named attributes (result.row_count)
        rather than positional indices (result[1]).
    CleanResult dataclass:
        Same pattern for clean(). The duplicate_rows_removed count is needed
        by complete_job() to store in PostgreSQL.
    utf-8-sig encoding:
        Python's "utf-8-sig" codec reads UTF-8 files and silently strips the
        optional BOM (Byte Order Mark, \xef\xbb\xbf) if present. Some Windows
        tools (Excel, Notepad) add a BOM when saving UTF-8 files. Without this
        codec, the BOM would appear as the first character of the first column
        name: \ufeffname instead of name.
    csv.DictReader:
        Reads CSV rows as dicts keyed by the header column names. Field count
        validation (restval/extras detection) is done manually after reading
        because csv.DictReader fills missing fields with restval (default None)
        and puts extra fields in a special key — neither raises an error.
    csv.DictWriter:
        Writes list-of-dicts to CSV. extrasaction='ignore' tells DictWriter
        to silently skip any extra keys in a row dict (defensive programming).

Interview concept:
    "Why is trim-before-dedup order significant?"
    Two rows with values " Alice " and "Alice" are logically identical after
    normalization. If you dedup before trim, they appear distinct (different
    whitespace) and both are kept — a wrong result for a data-cleaning tool.
    Trim first ensures normalization happens before comparison.
"""

import csv
import io
from dataclasses import dataclass, field

from worker.exceptions import InvalidCSVError


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class AnalysisResult:
    """
    Result returned by parse_and_analyse().

    WHAT:
        A structured container for the metadata and parsed content of a valid
        CSV file. All fields are populated before the function returns.

    Attributes:
        column_names: Ordered list of header column names.
                      Example: ['id', 'name', 'email', 'age']
        column_count: Number of columns (len(column_names)).
                      Stored separately to avoid recomputing.
        row_count:    Number of data rows (header row NOT counted).
                      A header-only CSV has row_count=0.
        rows:         All data rows as list-of-dicts, keyed by column name.
                      Example: [{'id': '1', 'name': 'Alice', ...}, ...]
                      All values are strings (csv.reader returns strings;
                      type coercion is not the parser's responsibility).

    WHY A DATACLASS (NOT A DICT OR TUPLE):
        - Named attributes (result.row_count) are more readable than
          positional indices (result[2]) or string keys (result["row_count"]).
        - The type annotation (dataclass) documents the shape of the return
          value at the call site — the caller knows exactly what to expect.
        - Dataclasses generate __init__, __repr__, and __eq__ automatically,
          reducing boilerplate and making test assertions easy:
              assert result == AnalysisResult(column_names=['a','b'], ...)
    """

    column_names: list[str]
    column_count: int
    row_count: int
    rows: list[dict] = field(default_factory=list)


@dataclass
class CleanResult:
    """
    Result returned by clean().

    WHAT:
        A structured container for the output of the cleaning phase.

    Attributes:
        cleaned_rows:          List of row dicts after trimming and dedup.
        duplicate_rows_removed: How many rows were removed as duplicates.
                                Stored in PostgreSQL for reporting.

    WHY TRACK DUPLICATE COUNT:
        The user sees "N duplicate rows removed" in their job result. This
        count is stored in jobs.duplicate_rows_removed and displayed by the
        frontend. It is also useful for auditing: if 50,000 rows became 100
        after dedup, the user's data may have a structural problem.
    """

    cleaned_rows: list[dict]
    duplicate_rows_removed: int


# ── Core functions ─────────────────────────────────────────────────────────────

def parse_and_analyse(file_path: str) -> AnalysisResult:
    """
    Read, validate, and parse a CSV file from disk.

    WHAT:
        Opens a CSV file, enforces the AD-21 validation contract, and returns
        an AnalysisResult containing the column metadata and all parsed rows.

    WHY:
        The Lambda worker must validate the CSV before processing it. If the
        file is invalid, there is no point downloading it, parsing it, or
        storing a partial result. Validation happens first, before any output
        is written, so that failures are detected early and cleanly.

    HOW (execution order):
        1. Check file size — raise InvalidCSVError if the file is 0 bytes.
        2. Open the file with utf-8-sig encoding (strips BOM if present).
        3. Wrap the file in csv.reader.
        4. Read the header row — the first row from csv.reader.
           Raise InvalidCSVError if:
             - The CSV has no rows at all (empty or whitespace-only after BOM)
             - Any header name is empty (empty string after strip)
             - Any header name is duplicated
        5. Read all remaining rows.
           Raise InvalidCSVError if any row has a different field count than
           the header row (too many OR too few fields).
        6. Build and return an AnalysisResult.

    Inputs:
        file_path: Path to a local CSV file (e.g. "/tmp/jobs/abc123/input.csv").
                   The file must be readable.

    Output:
        AnalysisResult with column_names, column_count, row_count, rows.

    Side effects:
        Reads the file from disk. No network calls. No database calls.

    Edge cases:
        - Empty file (0 bytes): raises InvalidCSVError("File is empty")
        - File with only a BOM and no content: treated as empty after BOM strip.
        - Invalid UTF-8 bytes: UnicodeDecodeError caught, re-raised as
          InvalidCSVError("File encoding is not valid UTF-8: ...")
        - Malformed CSV quoting: csv.reader raises csv.Error, re-raised as
          InvalidCSVError("Malformed CSV: ...")
        - Header-only file (no data rows): valid — returns row_count=0.
        - Row with wrong field count: raises InvalidCSVError with the row
          number, actual count, expected count, and header names listed.

    Analogy:
        parse_and_analyse() is the quality inspector at the warehouse receiving
        dock. Before any boxes (rows) enter the warehouse, the inspector checks:
        - Did a box arrive at all? (empty file check)
        - Are all shelf labels legible? (header validation)
        - Does each box have the right number of compartments? (field count check)
        Only after passing inspection does the cargo proceed to the factory floor.
    """
    import os

    # ── Step 1: Empty file guard ───────────────────────────────────────────────
    # Check file size before attempting to open and parse. An empty file would
    # produce zero rows from csv.reader, which we'd have to distinguish from a
    # header-only CSV (valid) — confusing. The 0-byte check is explicit and fast.
    file_size = os.path.getsize(file_path)
    if file_size == 0:
        raise InvalidCSVError("File is empty")

    try:
        # ── Step 2: Open with utf-8-sig encoding ──────────────────────────────
        # "utf-8-sig" is a Python codec alias for "UTF-8 with optional BOM".
        # If the file starts with the UTF-8 BOM (\xef\xbb\xbf), the codec
        # strips it before returning any characters to the reader.
        # If the file is plain UTF-8 with no BOM, it behaves identically to
        # the "utf-8" codec — no performance penalty.
        #
        # WHY NOT just open with "utf-8" and strip manually?
        # The BOM may be embedded as \ufeff in the decoded string. If we opened
        # with "utf-8" and the file had a BOM, the first column name would be
        # "\ufeffname" instead of "name". This would break every lookup on that
        # column. utf-8-sig handles this transparently at the codec level.
        #
        # newline="" is required by Python's csv module documentation.
        # The csv module handles line endings (CRLF, LF, mixed) internally.
        # If newline="" is not set, Python's universal newline translation
        # would convert CRLF to LF before csv.reader sees them — which would
        # break quoted fields that span multiple lines.
        with open(file_path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)

            # ── Step 3: Read the header row ────────────────────────────────────
            # next() on csv.reader returns the first row as a list of strings,
            # or raises StopIteration if the file has no rows at all.
            try:
                raw_headers = next(reader)
            except StopIteration:
                # The file had content (passed the 0-byte check) but csv.reader
                # found no rows — for example, the file contains only whitespace
                # or only a BOM. Treat this as effectively empty.
                raise InvalidCSVError("File is empty")

            # ── Step 4a: Validate — no empty header names ──────────────────────
            # An empty column name makes the row dict unworkable: every row
            # would have a key "" which is ambiguous and unexpected.
            # csv.reader returns empty strings for empty fields.
            for position, header in enumerate(raw_headers):
                if header.strip() == "":
                    raise InvalidCSVError(
                        f"Column has an empty name at position {position}"
                    )

            # ── Step 4b: Validate — no duplicate header names ──────────────────
            # Duplicate headers cause csv.DictReader to silently drop earlier
            # values (the last value wins). For a data cleaning tool, this is
            # unacceptable — it constitutes silent data loss. AD-21 (revised).
            # We detect duplicates by comparing the header list against a set.
            seen: set[str] = set()
            for header in raw_headers:
                if header in seen:
                    count = raw_headers.count(header)
                    raise InvalidCSVError(
                        f"Duplicate column name: '{header}' appears {count} times"
                    )
                seen.add(header)

            column_names = raw_headers
            expected_field_count = len(column_names)

            # ── Step 5: Read all data rows ─────────────────────────────────────
            rows: list[dict] = []
            for row_index, raw_row in enumerate(reader, start=1):
                # Validate field count: each row must have exactly as many
                # fields as the header row. csv.reader already handles quoted
                # commas (they don't count as field separators), so a mismatch
                # here means the file is genuinely malformed.
                actual_field_count = len(raw_row)
                if actual_field_count != expected_field_count:
                    raise InvalidCSVError(
                        f"Row {row_index} has {actual_field_count} fields; "
                        f"expected {expected_field_count} "
                        f"(headers: {column_names})"
                    )
                # Build the row dict by zipping column names with field values.
                # This mirrors what csv.DictReader does internally, but we do it
                # manually so we can control validation precisely.
                rows.append(dict(zip(column_names, raw_row)))

    except UnicodeDecodeError as exc:
        # The file contains bytes that are not valid UTF-8. This is a permanent
        # failure — the file cannot be processed regardless of retry count.
        # Include the exception message so operators can see which byte offset
        # triggered the error.
        raise InvalidCSVError(f"File encoding is not valid UTF-8: {exc}") from exc

    except csv.Error as exc:
        # csv.reader raises csv.Error for malformed CSV (e.g., an unclosed
        # quote in a quoted field). This is a permanent failure.
        raise InvalidCSVError(f"Malformed CSV: {exc}") from exc

    return AnalysisResult(
        column_names=column_names,
        column_count=len(column_names),
        row_count=len(rows),
        rows=rows,
    )


def clean(rows: list[dict]) -> CleanResult:
    """
    Trim whitespace and remove duplicate rows from a list of row dicts.

    WHAT:
        Applies two cleaning operations in order:
          1. TRIM: strip leading and trailing whitespace from every string value.
          2. DEDUP: remove rows that are identical after trimming.
        Returns the cleaned rows and the count of duplicates removed.

    WHY TRIM BEFORE DEDUP:
        Consider two rows: {'name': 'Alice '} and {'name': 'Alice'}.
        Before trimming, these rows are distinct (different strings).
        After trimming, they are identical.

        If we deduplicated BEFORE trimming, we would keep both rows — the
        untrimmed "Alice " would survive as a "unique" row even though it
        represents the same data as "Alice". The cleaned output would contain
        "Alice " (with trailing space) and "Alice" — both preserved.

        If we trim FIRST, "Alice " becomes "Alice", then dedup finds two
        identical rows and removes one. The cleaned output contains exactly
        one "Alice" — correct behaviour for a data-cleaning tool.

        The order enforces the invariant: deduplication is always based on the
        NORMALIZED (trimmed) form of values, not the raw form.

    HOW (execution order):
        1. Iterate over each row dict.
           For each row, create a new dict where every string value has
           leading/trailing whitespace stripped. Non-string values are
           passed through unchanged (defensive — all CSV values are strings,
           but the function signature accepts dict to be composable).
        2. Deduplicate the trimmed rows.
           Representation: convert each row dict to a tuple of (key, value)
           pairs (frozenset is not used because column order matters for CSV
           output). Track seen tuples in a set; keep only first occurrences.
        3. Return CleanResult with cleaned rows and duplicate count.

    Inputs:
        rows: List of row dicts as produced by parse_and_analyse().
              All values are expected to be strings (CSV rows).
              Each dict has the same set of keys (the header column names).

    Output:
        CleanResult(cleaned_rows=..., duplicate_rows_removed=...)
        cleaned_rows may be shorter than `rows` if duplicates were found.
        duplicate_rows_removed = len(rows) - len(cleaned_rows).

    Side effects:
        None — pure function. Does not modify the input list.
        Creates new dict objects for the trimmed rows.

    Edge cases:
        - Empty rows list: returns CleanResult(cleaned_rows=[], duplicate_rows_removed=0).
        - All rows identical: returns CleanResult with one row, rest removed.
        - No duplicates: cleaned_rows == trimmed version of rows; duplicate_rows_removed=0.
        - Rows with non-string values: non-strings passed through (str.strip()
          only called on str instances). In practice, all CSV values are strings.

    Interview concept:
        "Why does the order of trim vs dedup matter in data cleaning pipelines?"
        Normalization (trim, lowercase, canonicalize) must always precede
        deduplication. Otherwise, logically identical records that differ only
        in formatting (trailing spaces, different cases) will pass through as
        distinct records. The pipeline is "normalize → compare → deduplicate",
        never "compare → normalize".
    """
    # ── Phase 1: Trim ─────────────────────────────────────────────────────────
    # Create a new list of trimmed row dicts. We do not mutate the input list
    # in case the caller still needs the original rows (pure function principle).
    trimmed_rows: list[dict] = []
    for row in rows:
        trimmed_row = {
            key: value.strip() if isinstance(value, str) else value
            for key, value in row.items()
        }
        trimmed_rows.append(trimmed_row)

    # ── Phase 2: Deduplicate ──────────────────────────────────────────────────
    # We need an ordered deduplication that:
    #   - Keeps the FIRST occurrence of each distinct row.
    #   - Removes all subsequent occurrences.
    #   - Preserves the original order of unique rows.
    #
    # APPROACH: Convert each row dict to a tuple of (key, value) pairs.
    # Tuples are hashable and can be stored in a set. Because dicts in Python
    # 3.7+ preserve insertion order, dict.items() returns pairs in column order.
    # This means two rows with the same keys and values (in the same order)
    # produce identical tuples, which the set detects as duplicates.
    #
    # WHY NOT frozenset(row.items())?
    # A frozenset is unordered — it would treat {'a': '1', 'b': '2'} and
    # {'b': '2', 'a': '1'} as identical. Since CSV column order is significant
    # (and consistent for all rows from the same file), using a tuple (ordered)
    # is correct.
    seen_tuples: set[tuple] = set()
    deduplicated_rows: list[dict] = []

    for row in trimmed_rows:
        row_key = tuple(row.items())
        if row_key not in seen_tuples:
            seen_tuples.add(row_key)
            deduplicated_rows.append(row)
        # If row_key IS in seen_tuples, this is a duplicate — skip it.

    duplicate_rows_removed = len(trimmed_rows) - len(deduplicated_rows)

    return CleanResult(
        cleaned_rows=deduplicated_rows,
        duplicate_rows_removed=duplicate_rows_removed,
    )


def write_csv(file_path: str, column_names: list[str], rows: list[dict]) -> None:
    """
    Write a list of row dicts to a CSV file using csv.DictWriter.

    WHAT:
        Opens `file_path` for writing and writes a CSV with the given column
        names as the header row, followed by one row per dict in `rows`.

    WHY:
        The Lambda worker needs to write the cleaned CSV to /tmp/ before
        uploading it to S3. This function encapsulates the DictWriter setup
        so the handler stays clean. It also enforces consistent CSV formatting
        (lineterminator, extrasaction) in one place.

    HOW (execution order):
        1. Open the file at file_path for writing with UTF-8 encoding
           and newline="" (required by csv module).
        2. Create a csv.DictWriter with the given fieldnames.
        3. Write the header row (writeheader()).
        4. Write all data rows (writerows(rows)).
        5. File is closed automatically by the context manager.

    Inputs:
        file_path:    Path to write the output CSV (e.g. "/tmp/abc123_result.csv").
                      Parent directory must exist.
        column_names: List of column header names in order.
                      These become the CSV header row.
        rows:         List of row dicts. Each dict should have the same keys
                      as column_names, but extrasaction='ignore' handles extras.

    Output:
        None. Raises OSError if the file cannot be written.

    Side effects:
        Creates or overwrites the file at file_path.
        The file is always UTF-8 encoded (no BOM written — consumers
        should handle both BOM and non-BOM UTF-8 as per AD-21).

    Edge cases:
        - Empty rows list: writes a header-only CSV (valid per AD-21 contract).
        - Rows with extra keys: ignored (extrasaction='ignore').
        - Rows with missing keys: DictWriter writes an empty string for missing
          fields (the default restval="" behavior).

    Interview concept:
        "Why use extrasaction='ignore' in DictWriter?"
        By default, DictWriter raises ValueError if a row dict contains keys
        not in fieldnames. Setting extrasaction='ignore' silently skips extra
        keys. This is defensive: if the row dicts somehow have extra keys
        (e.g., from a future change to parse_and_analyse), the writer does
        not fail — it just omits the extra columns from the output. For a
        data-cleaning tool that should be robust, this is preferable to crashing.
    """
    with open(file_path, mode="w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=column_names,
            # lineterminator="\r\n" produces Windows-style CRLF line endings,
            # which is the standard for CSV files per RFC 4180. Most tools
            # (Excel, Google Sheets) handle both CRLF and LF, but CRLF is
            # the technically correct choice for CSV output.
            lineterminator="\r\n",
            # extrasaction='ignore': if a row dict has keys not in fieldnames,
            # silently ignore them rather than raising ValueError. Defensive
            # against unexpected dict contents — see function docstring.
            extrasaction="ignore",
        )
        # Write the header row first (the column names line).
        writer.writeheader()
        # Write all data rows. writerows() iterates and calls writerow() for each.
        writer.writerows(rows)

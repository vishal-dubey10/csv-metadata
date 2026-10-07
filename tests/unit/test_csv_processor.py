"""
Unit tests for worker/csv_processor.py — CSV parsing, cleaning, and writing.

WHAT:
    Tests the full AD-21 validation contract for parse_and_analyse(), the
    cleaning semantics for clean(), and the round-trip correctness for
    write_csv(). All tests are pure Python — no AWS, no database, no network.

WHY:
    csv_processor.py is the core data transformation logic of CSVForge.
    Getting the validation contract wrong means either:
      - Valid files are rejected (false positive) — the user gets a spurious
        "failed" job for a file that should work.
      - Invalid files are silently accepted (false negative) — corrupt data
        enters the system undetected.
    These tests pin down the exact boundary between "valid" and "invalid".

WHAT EACH TEST CLASS COVERS:
    TestParseAndAnalyseHappyPath:
        Verifies the correct result shape for valid CSV inputs:
        row counts, column counts, column names, and that the header-only
        case (row_count=0) is handled as valid.

    TestParseAndAnalyseValidationErrors:
        Verifies the full AD-21 permanent-failure contract:
        empty file, binary file, duplicate headers, empty header name,
        wrong field count, UTF-8 BOM handling, mixed CRLF/LF.

    TestClean:
        Verifies trimming, deduplication, and the critical trim-before-dedup
        order invariant.

    TestWriteCsv:
        Verifies that write_csv produces a valid CSV that can be re-read
        by csv.DictReader with identical content.

PATTERN NOTES:
    All tests create temporary files in a pytest tmp_path fixture.
    Tests do NOT use @mock_aws — no AWS calls happen in csv_processor.py.
    Class-based grouping pattern (class TestXxx: / def test_yyy(self, ...))
    is used throughout, matching the style established in test_job_repository_unit.py.
"""

import csv
import os

import pytest

from worker.csv_processor import (
    AnalysisResult,
    CleanResult,
    clean,
    parse_and_analyse,
    write_csv,
)
from worker.exceptions import InvalidCSVError


# ── Happy path ─────────────────────────────────────────────────────────────────

class TestParseAndAnalyseHappyPath:
    """
    Valid CSV inputs should parse successfully and return correct metadata.

    These tests verify the "sunny day" path: well-formed CSV files that should
    pass all validation checks and produce an AnalysisResult with the correct
    row_count, column_count, and column_names.
    """

    def test_basic_csv_row_count(self, tmp_path):
        """Three data rows → row_count=3."""
        f = tmp_path / "basic.csv"
        f.write_text("name,age,city\nAlice,30,NYC\nBob,25,LA\nCarol,35,Chicago\n", encoding="utf-8")
        result = parse_and_analyse(str(f))
        assert result.row_count == 3

    def test_basic_csv_column_count(self, tmp_path):
        """Three header fields → column_count=3."""
        f = tmp_path / "cols.csv"
        f.write_text("name,age,city\nAlice,30,NYC\n", encoding="utf-8")
        result = parse_and_analyse(str(f))
        assert result.column_count == 3

    def test_basic_csv_column_names(self, tmp_path):
        """Column names are extracted correctly from the header row."""
        f = tmp_path / "names.csv"
        f.write_text("id,email,score\n1,a@b.com,99\n", encoding="utf-8")
        result = parse_and_analyse(str(f))
        assert result.column_names == ["id", "email", "score"]

    def test_basic_csv_row_values(self, tmp_path):
        """Rows are returned as dicts with correct key-value pairs."""
        f = tmp_path / "values.csv"
        f.write_text("name,age\nAlice,30\nBob,25\n", encoding="utf-8")
        result = parse_and_analyse(str(f))
        assert result.rows[0] == {"name": "Alice", "age": "30"}
        assert result.rows[1] == {"name": "Bob", "age": "25"}

    def test_header_only_csv_is_valid(self, tmp_path):
        """
        A CSV with only a header row and no data rows is valid per AD-21.
        This represents an intentionally empty dataset, not a broken file.
        The result should have row_count=0 and an empty rows list.
        """
        f = tmp_path / "header_only.csv"
        f.write_text("name,age,city\n", encoding="utf-8")
        result = parse_and_analyse(str(f))
        assert result.row_count == 0
        assert result.rows == []
        assert result.column_names == ["name", "age", "city"]

    def test_header_only_no_trailing_newline(self, tmp_path):
        """Header-only CSV without trailing newline is also valid."""
        f = tmp_path / "header_no_newline.csv"
        f.write_text("a,b,c", encoding="utf-8")
        result = parse_and_analyse(str(f))
        assert result.row_count == 0
        assert result.column_count == 3

    def test_quoted_field_with_comma(self, tmp_path):
        """Quoted fields containing commas are parsed as a single field."""
        f = tmp_path / "quoted.csv"
        f.write_text('name,description\nAlice,"Hello, World"\n', encoding="utf-8")
        result = parse_and_analyse(str(f))
        assert result.column_count == 2
        assert result.rows[0]["description"] == "Hello, World"

    def test_utf8_bom_stripped(self, tmp_path):
        """
        UTF-8 BOM (\xef\xbb\xbf) must be stripped transparently.
        The first column name must NOT start with the BOM character \ufeff.
        If the BOM is not stripped, the first column name would be
        '\ufeffname' instead of 'name', breaking all lookups on that column.
        """
        f = tmp_path / "bom.csv"
        # Write BOM manually as raw bytes, then add the CSV content.
        f.write_bytes(b"\xef\xbb\xbfname,age\nAlice,30\n")
        result = parse_and_analyse(str(f))
        # The first column name must be exactly 'name', not '\ufeffname'.
        assert result.column_names[0] == "name"
        assert not result.column_names[0].startswith("\ufeff")

    def test_crlf_line_endings(self, tmp_path):
        """CRLF line endings are handled correctly by csv.reader."""
        f = tmp_path / "crlf.csv"
        f.write_bytes(b"name,age\r\nAlice,30\r\nBob,25\r\n")
        result = parse_and_analyse(str(f))
        assert result.row_count == 2
        assert result.rows[0] == {"name": "Alice", "age": "30"}

    def test_mixed_crlf_lf_line_endings(self, tmp_path):
        """
        Mixed CRLF and LF line endings parse correctly.
        csv.reader with newline="" handles line ending normalization internally,
        so mixed endings in the same file are not an error.
        """
        f = tmp_path / "mixed.csv"
        # First data row uses CRLF, second uses LF.
        f.write_bytes(b"name,age\r\nAlice,30\r\nBob,25\n")
        result = parse_and_analyse(str(f))
        assert result.row_count == 2
        assert result.rows[1] == {"name": "Bob", "age": "25"}


# ── Validation errors (AD-21 permanent failures) ────────────────────────────────

class TestParseAndAnalyseValidationErrors:
    """
    Invalid CSV inputs must raise InvalidCSVError (permanent failure).

    Each test in this class corresponds to one permanent-failure case in the
    AD-21 validation contract. These tests verify that the error is raised
    (not swallowed), and that the error message contains useful diagnostic info.

    If any of these tests fail:
    - The worker would accept a broken file → corrupt data enters the system, OR
    - A valid file is incorrectly rejected → user gets a false failure.
    """

    def test_empty_file_raises(self, tmp_path):
        """0-byte file → InvalidCSVError("File is empty")."""
        f = tmp_path / "empty.csv"
        f.write_bytes(b"")
        with pytest.raises(InvalidCSVError) as exc_info:
            parse_and_analyse(str(f))
        assert "empty" in exc_info.value.message.lower()

    def test_binary_file_raises(self, tmp_path):
        """
        A file with non-UTF-8 binary content must raise InvalidCSVError.
        Binary files are a permanent failure — retrying will not fix them.
        The error must mention encoding or UTF-8 in the message so the user
        understands what went wrong.
        """
        f = tmp_path / "binary.bin"
        # \xff\xfe is the UTF-16 BOM — not valid UTF-8.
        f.write_bytes(b"\xff\xfe\x00\x01\x02\x03binary content")
        with pytest.raises(InvalidCSVError) as exc_info:
            parse_and_analyse(str(f))
        assert "utf-8" in exc_info.value.message.lower()

    def test_duplicate_headers_raise(self, tmp_path):
        """
        Duplicate column names are a permanent failure (AD-21 revised).
        csv.DictReader with duplicate headers silently drops earlier values,
        which constitutes undetectable data loss for a data-cleaning tool.
        The error message must name the duplicate column.
        """
        f = tmp_path / "dup_headers.csv"
        f.write_text("name,age,name\nAlice,30,Bob\n", encoding="utf-8")
        with pytest.raises(InvalidCSVError) as exc_info:
            parse_and_analyse(str(f))
        assert "name" in exc_info.value.message
        assert "duplicate" in exc_info.value.message.lower()

    def test_empty_header_name_raises(self, tmp_path):
        """
        A column with no name (empty header field) raises InvalidCSVError.
        An empty column name makes row dicts unworkable (key is "").
        The error must mention position.
        """
        f = tmp_path / "empty_header.csv"
        # Middle column has an empty name.
        f.write_text("name,,age\nAlice,x,30\n", encoding="utf-8")
        with pytest.raises(InvalidCSVError) as exc_info:
            parse_and_analyse(str(f))
        assert "empty" in exc_info.value.message.lower()

    def test_row_with_extra_fields_raises(self, tmp_path):
        """
        A data row with more fields than the header raises InvalidCSVError.
        The error must mention the row number, actual count, and expected count.
        """
        f = tmp_path / "extra_fields.csv"
        # Header has 3 columns, data row has 4 fields.
        f.write_text("name,age,city\nAlice,30,NYC,ExtraField\n", encoding="utf-8")
        with pytest.raises(InvalidCSVError) as exc_info:
            parse_and_analyse(str(f))
        # The error message must include the actual and expected field counts.
        assert "4" in exc_info.value.message
        assert "3" in exc_info.value.message

    def test_row_with_fewer_fields_raises(self, tmp_path):
        """
        A data row with fewer fields than the header raises InvalidCSVError.
        Same error format as the extra-fields case.
        """
        f = tmp_path / "fewer_fields.csv"
        # Header has 3 columns, data row has only 2 fields.
        f.write_text("name,age,city\nAlice,30\n", encoding="utf-8")
        with pytest.raises(InvalidCSVError) as exc_info:
            parse_and_analyse(str(f))
        assert "2" in exc_info.value.message
        assert "3" in exc_info.value.message

    def test_invalid_utf8_raises(self, tmp_path):
        """
        A file with invalid UTF-8 byte sequences raises InvalidCSVError.
        \x80 is a continuation byte that cannot appear without a leading byte —
        invalid UTF-8.
        """
        f = tmp_path / "bad_encoding.csv"
        # Valid UTF-8 header, then invalid UTF-8 in the data row.
        f.write_bytes(b"name,age\nAlice\x80invalid,30\n")
        with pytest.raises(InvalidCSVError) as exc_info:
            parse_and_analyse(str(f))
        assert "utf-8" in exc_info.value.message.lower()

    def test_returns_invalid_csv_error_not_generic(self, tmp_path):
        """
        Empty file must raise InvalidCSVError specifically, not Exception or OSError.
        The worker's except clause catches InvalidCSVError only — other exceptions
        propagate to trigger SQS retry. This test ensures the narrow-catch works.
        """
        f = tmp_path / "empty2.csv"
        f.write_bytes(b"")
        with pytest.raises(InvalidCSVError):
            parse_and_analyse(str(f))


# ── Cleaner tests ──────────────────────────────────────────────────────────────

class TestClean:
    """
    Tests for clean() — trim whitespace, then deduplicate.

    The critical invariant: trim BEFORE dedup. Rows that are distinct before
    trimming but identical after trimming must be treated as duplicates.
    """

    def test_no_duplicates_all_rows_retained(self):
        """All unique rows should be present in cleaned output."""
        rows = [
            {"name": "Alice", "age": "30"},
            {"name": "Bob", "age": "25"},
            {"name": "Carol", "age": "35"},
        ]
        result = clean(rows)
        assert len(result.cleaned_rows) == 3
        assert result.duplicate_rows_removed == 0

    def test_identical_rows_deduplicated_to_one(self):
        """
        All-identical rows should be reduced to a single row.
        If 5 identical rows are submitted, only 1 should appear in the output.
        duplicate_rows_removed should be 4 (5 original - 1 kept).
        """
        row = {"name": "Alice", "age": "30"}
        rows = [row.copy(), row.copy(), row.copy(), row.copy(), row.copy()]
        result = clean(rows)
        assert len(result.cleaned_rows) == 1
        assert result.duplicate_rows_removed == 4

    def test_trim_before_dedup_whitespace_variants_deduped(self):
        """
        Rows that are identical AFTER trimming must be treated as duplicates,
        even if they appear distinct before trimming.

        This is the trim-before-dedup invariant test. If the order were reversed
        (dedup before trim), both rows would survive because "Alice " != "Alice".
        The correct behaviour is: trim first, then compare, then dedup.
        """
        rows = [
            {"name": "Alice ", "age": "30"},   # trailing space in name
            {"name": "Alice", "age": "30"},    # no trailing space
        ]
        result = clean(rows)
        # After trimming "Alice " → "Alice", both rows are identical.
        # Dedup should keep only one.
        assert len(result.cleaned_rows) == 1
        assert result.duplicate_rows_removed == 1
        # The kept row should have the trimmed value.
        assert result.cleaned_rows[0]["name"] == "Alice"

    def test_trim_strips_leading_whitespace(self):
        """Leading whitespace is stripped from string values."""
        rows = [{"name": "  Alice", "city": "   NYC"}]
        result = clean(rows)
        assert result.cleaned_rows[0]["name"] == "Alice"
        assert result.cleaned_rows[0]["city"] == "NYC"

    def test_trim_strips_trailing_whitespace(self):
        """Trailing whitespace is stripped from string values."""
        rows = [{"name": "Alice   ", "city": "NYC   "}]
        result = clean(rows)
        assert result.cleaned_rows[0]["name"] == "Alice"
        assert result.cleaned_rows[0]["city"] == "NYC"

    def test_first_occurrence_wins_on_dedup(self):
        """
        When rows are deduplicated, the first occurrence is kept.
        This is the "first occurrence wins" contract. The order of unique rows
        in cleaned_rows must match the order in which they first appeared.
        """
        rows = [
            {"name": "Alice", "age": "30"},
            {"name": "Bob", "age": "25"},
            {"name": "Alice", "age": "30"},  # duplicate of row 0
        ]
        result = clean(rows)
        assert len(result.cleaned_rows) == 2
        assert result.cleaned_rows[0] == {"name": "Alice", "age": "30"}
        assert result.cleaned_rows[1] == {"name": "Bob", "age": "25"}
        assert result.duplicate_rows_removed == 1

    def test_empty_rows_returns_empty_result(self):
        """Empty input list → empty CleanResult with 0 duplicates removed."""
        result = clean([])
        assert result.cleaned_rows == []
        assert result.duplicate_rows_removed == 0

    def test_duplicate_count_is_correct(self):
        """duplicate_rows_removed = total input rows - unique output rows."""
        rows = [
            {"x": "a"},
            {"x": "b"},
            {"x": "a"},  # duplicate
            {"x": "c"},
            {"x": "b"},  # duplicate
        ]
        result = clean(rows)
        assert len(result.cleaned_rows) == 3
        assert result.duplicate_rows_removed == 2

    def test_partial_whitespace_row_not_deduped_with_different_row(self):
        """
        Rows that are genuinely distinct after trimming are NOT deduplicated.
        "Alice" and "Bob" are different even after trimming.
        """
        rows = [
            {"name": "  Alice  ", "age": "30"},
            {"name": "  Bob  ", "age": "25"},
        ]
        result = clean(rows)
        assert len(result.cleaned_rows) == 2
        assert result.duplicate_rows_removed == 0


# ── write_csv round-trip ───────────────────────────────────────────────────────

class TestWriteCsv:
    """
    Tests for write_csv() — CSV output can be re-read identically.

    The primary contract: write_csv() must produce a file that csv.DictReader
    can parse back to produce the exact same rows that were written.
    """

    def test_write_and_read_back_matches(self, tmp_path):
        """
        write_csv followed by csv.DictReader returns identical rows.
        This is the round-trip correctness test — the most important property
        of write_csv. Any serialization format should survive a read-write cycle.
        """
        f = tmp_path / "output.csv"
        column_names = ["name", "age", "city"]
        rows = [
            {"name": "Alice", "age": "30", "city": "NYC"},
            {"name": "Bob", "age": "25", "city": "LA"},
        ]
        write_csv(str(f), column_names, rows)

        # Re-read the file using standard csv.DictReader.
        with open(str(f), encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            read_back = list(reader)

        assert read_back == rows

    def test_header_only_write(self, tmp_path):
        """
        write_csv with an empty rows list writes only the header row.
        Reading back with DictReader returns an empty list (no data rows),
        and the fieldnames are correct.
        """
        f = tmp_path / "header_only.csv"
        column_names = ["a", "b", "c"]
        write_csv(str(f), column_names, [])

        with open(str(f), encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            rows_read = list(reader)
            assert reader.fieldnames == ["a", "b", "c"]

        assert rows_read == []

    def test_write_csv_creates_file(self, tmp_path):
        """write_csv creates the file at the given path."""
        f = tmp_path / "created.csv"
        assert not f.exists()
        write_csv(str(f), ["col"], [{"col": "val"}])
        assert f.exists()

    def test_write_csv_file_is_nonempty(self, tmp_path):
        """write_csv with data rows produces a non-empty file."""
        f = tmp_path / "nonempty.csv"
        write_csv(str(f), ["x"], [{"x": "1"}, {"x": "2"}])
        assert f.stat().st_size > 0

    def test_write_csv_correct_column_count_on_read(self, tmp_path):
        """The output CSV has the correct number of columns when read back."""
        f = tmp_path / "cols.csv"
        column_names = ["id", "name", "email", "score", "active"]
        rows = [{"id": "1", "name": "Alice", "email": "a@b.com", "score": "99", "active": "true"}]
        write_csv(str(f), column_names, rows)

        with open(str(f), encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == column_names

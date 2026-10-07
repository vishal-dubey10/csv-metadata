"""
CSVForge worker — custom exception types.

WHAT:
    Defines InvalidCSVError, the single exception type the CSV worker raises
    for permanently invalid input files. All other exceptions (network errors,
    database timeouts, S3 throttling) are left to propagate naturally.

WHY:
    The worker has two fundamentally different categories of failure:

    1. PERMANENT FAILURE — the file itself is broken and can never be processed:
       - The file is empty.
       - The file is not valid UTF-8.
       - The CSV has duplicate column names.
       - A data row has the wrong number of fields.
       In these cases, retrying is pointless. The worker should mark the job
       as failed in PostgreSQL and return cleanly. SQS will delete the message
       because the Lambda function returned successfully (no exception raised).

    2. TRANSIENT FAILURE — a temporary infrastructure problem that may resolve:
       - S3 is throttling requests.
       - The PostgreSQL connection timed out.
       - A network packet was dropped.
       In these cases, retrying is correct. The worker raises the exception,
       Lambda propagates it to SQS, the message becomes visible again after the
       visibility timeout (1800s), and Lambda retries up to MaxReceiveCount=5
       times before routing the message to the DLQ.

ARCHITECTURE:

    Worker receives SQS message
          │
          ▼
    parse CSV (csv_processor.py)
          │
          ├── InvalidCSVError raised?
          │       YES → catch it, call fail_job(), return normally
          │              → SQS deletes the message (function returned cleanly)
          │              → job is marked failed in PostgreSQL
          │
          └── Other exception raised (S3, DB, network)?
                  YES → let it propagate out of the Lambda handler
                         → Lambda signals failure to SQS
                         → SQS makes message visible again after visibility timeout
                         → Lambda retries the job (up to MaxReceiveCount times)
                         → After MaxReceiveCount failures, message moves to DLQ

PERMANENT vs TRANSIENT DISTINCTION:
    This distinction is the core insight of the worker's error-handling design.
    An InvalidCSVError signals "this specific file is broken" — a property that
    will not change if you retry. A transient exception signals "the environment
    has a temporary problem" — which may resolve on retry.

    Getting this distinction wrong has real consequences:
    - Treating a permanent failure as transient: the job retries 5 times,
      fails each time, and ends up in the DLQ — wasting Lambda invocations
      and delaying the user's "failed" status notification.
    - Treating a transient failure as permanent: a job that could have
      succeeded after a brief S3 outage is immediately marked failed,
      giving the user a false failure.

    See AD-21 in docs/DECISIONS.md for the full validation contract.

IMPORTANT CONCEPTS:
    InvalidCSVError.message:
        The `.message` attribute (not `.args[0]`) stores the human-readable
        description of what went wrong. This message is stored in the PostgreSQL
        `jobs.failure_reason` column and surfaced to the user.
    Exception subclassing:
        `class InvalidCSVError(Exception)` inherits all Python exception
        machinery (traceback, raise, except clauses). We add only the `.message`
        attribute to carry the user-facing description.
    SQS delete-on-success contract:
        SQS Standard queues delete a message only when the Lambda function that
        received it returns successfully (exit code 0, no exception). If the
        function raises an uncaught exception, SQS redelivers the message after
        the visibility timeout. This is why the worker catches InvalidCSVError
        and returns cleanly — to tell SQS "I processed this message, delete it."

Interview concept:
    "How do you design a reliable worker for at-least-once delivery queues?"
    The worker must distinguish permanent from transient failures. For permanent
    failures (invalid input), catch the exception, update the job status, and
    return cleanly — the message is acknowledged and deleted. For transient
    failures (infrastructure problems), let the exception propagate — SQS will
    retry automatically. This design avoids infinite retry loops on bad data
    while still benefiting from automatic retry on recoverable errors.
"""


class InvalidCSVError(Exception):
    """
    Raised when a CSV file has a permanent, unrecoverable validation failure.

    WHAT:
        An exception that carries a human-readable description of WHY the CSV
        is invalid. Raised by csv_processor.parse_and_analyse() when the file
        cannot be processed under any circumstances.

    WHY THIS IS A SEPARATE EXCEPTION CLASS (NOT ValueError/IOError):
        Using a dedicated exception type allows the worker handler to catch
        EXACTLY the errors that represent permanent CSV failures, without
        accidentally catching other exceptions (like ValueError from a bug
        in the processing code, or IOError from a disk issue).

        Compare:
            except ValueError:    # would catch bugs in our own code too
            except InvalidCSVError:  # catches ONLY permanent CSV failures

        This is the "narrow catch" principle: always catch the most specific
        exception type that represents the condition you want to handle.

    PERMANENT FAILURE CONTRACT:
        When InvalidCSVError is raised:
          1. The worker catches it.
          2. The worker calls fail_job(db, job_id, error.message).
          3. The worker returns normally (no re-raise).
          4. SQS deletes the message (because Lambda returned without error).
          5. The user sees the job status as "failed" with failure_reason set.

        The worker MUST NOT re-raise InvalidCSVError — doing so would cause
        SQS to retry the message with the same broken file, looping until
        MaxReceiveCount is exhausted and the message goes to the DLQ.

    Inputs to __init__:
        message: Human-readable description of the validation failure.
                 This string is stored in jobs.failure_reason in PostgreSQL
                 and surfaced to the user. Write for an end user, not a developer.
                 Examples:
                   "File is empty"
                   "Duplicate column name: 'email' appears 2 times"
                   "Row 3 has 5 fields; expected 4 (headers: ['id', 'name', 'age', 'city'])"

    Attributes:
        message: str — the human-readable failure description.
    """

    def __init__(self, message: str) -> None:
        # Call the parent Exception.__init__ so this exception behaves normally:
        # traceback rendering, `raise ... from ...`, `str(error)`, etc.
        super().__init__(message)

        # Store the message as a named attribute for easy access by the caller.
        # The worker accesses error.message (not str(error) or error.args[0])
        # for clarity. Both are equivalent because super().__init__(message)
        # sets args[0] = message, and str(exception) = args[0] for single-arg
        # exceptions — but the named attribute makes the intent explicit.
        self.message = message

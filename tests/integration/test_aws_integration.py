"""
Real AWS integration tests.
These tests use REAL AWS resources and require valid credentials.
Run with: pytest -m aws -v
NEVER run as part of normal test suite — always excluded by default.

WHY these tests exist:
Unit tests use moto (mock AWS). These tests verify that the mocks
accurately represent real AWS behavior. Run them before deploying to production
or after changing IAM policies.
"""
import pytest
import boto3
import os
from botocore.exceptions import ClientError

pytestmark = pytest.mark.aws

BUCKET_NAME = os.getenv('S3_BUCKET_NAME', 'csvforge-files-dev')
QUEUE_NAME = os.getenv('SQS_QUEUE_NAME', 'csvforge-jobs-dev')
REGION = os.getenv('AWS_DEFAULT_REGION', 'ap-south-1')


@pytest.mark.aws
def test_s3_bucket_accessible():
    """Verify S3 bucket exists and is reachable."""
    s3 = boto3.client('s3', region_name=REGION)
    response = s3.head_bucket(Bucket=BUCKET_NAME)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200


@pytest.mark.aws
def test_presigned_url_generation():
    """Generate a presigned PUT URL and verify its format."""
    s3 = boto3.client('s3', region_name=REGION)
    url = s3.generate_presigned_url(
        'put_object',
        Params={'Bucket': BUCKET_NAME, 'Key': 'uploads/test/presign-check.csv'},
        ExpiresIn=60
    )
    assert url.startswith('https://')
    assert BUCKET_NAME in url
    assert 'X-Amz-Signature' in url


@pytest.mark.aws
def test_sqs_queue_accessible():
    """Verify SQS queue exists and is reachable."""
    sqs = boto3.client('sqs', region_name=REGION)
    response = sqs.get_queue_url(QueueName=QUEUE_NAME)
    assert 'QueueUrl' in response
    assert QUEUE_NAME in response['QueueUrl']


@pytest.mark.aws
def test_end_to_end_manual():
    """
    Full end-to-end flow test (manual verification steps included as comments).
    This test documents the steps even if it only automates some of them.

    Manual steps to verify the complete pipeline:
    1. Call POST /uploads/intent to get a presigned URL
    2. PUT a CSV file to the presigned URL
    3. Call POST /uploads/{job_id}/complete
    4. Wait ~10 seconds for Lambda to process
    5. Call GET /jobs/{job_id} to check status == 'completed'
    6. Call GET /jobs/{job_id}/download to get presigned download URL
    7. Download the processed CSV and verify it is clean
    """
    # This test verifies that the SQS queue is reachable and accepts messages
    # (does not actually process a job — use the manual steps above for E2E)
    sqs = boto3.client('sqs', region_name=REGION)
    queue_url_response = sqs.get_queue_url(QueueName=QUEUE_NAME)
    queue_url = queue_url_response['QueueUrl']

    # Verify queue attributes (visibility timeout, DLQ configured)
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=['VisibilityTimeout', 'RedrivePolicy', 'ApproximateNumberOfMessages']
    )
    assert attrs['Attributes']['VisibilityTimeout'] == '1800', \
        f"Expected visibility timeout 1800s, got {attrs['Attributes']['VisibilityTimeout']}"
    assert 'RedrivePolicy' in attrs['Attributes'], \
        'Queue must have a DLQ configured (RedrivePolicy missing)'

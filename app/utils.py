# -------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Utility services (MINIO Storage & PDF Processing)
# -------------------------------------------------------------------------------------------------------------------------------------------

import boto3
from botocore.exceptions import ClientError
from app.config import settings
import logging

# Configure Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_s3_client():
    """
    Creates and returns a Boto3 client configured for MinIO
    """

    return boto3.client(
        's3',
        endpoint_url=settings.MINIO_ENDPOINT,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        # MinIO requires 's3v4' signature version to work correctly
        # with standard AWS SDKs locally.
        config=boto3.session.Config(signature_version='s3v4')
    )


def init_storage():
    """
    Checks if the S3 bucket exists. If not, creates it.
    Run this on startup
    """

    s3 = get_s3_client()
    bucket_name = settings.MINIO_BUCKET_NAME

    try:
        # Check if bucket exists by asking for its metadata (Head)
        s3.head_bucket(Bucket=bucket_name)
        logger.info(f"Storage: Bucket '{bucket_name}' already exists.")
    except ClientError:
        # If head_bucket fails, it implies the bucket is missing (or 403 Forbidden)
        try:
            s3.create_bucket(Bucket=bucket_name)
            logger.info(f"Storage: Bucket '{bucket_name}' created successfully.")
        except Exception as e:
            logger.error(f"Storage: Failed to create bucket. Error: {e}")



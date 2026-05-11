# ---------------------------------------------------------------------------
# Purpose: Utility services (MinIO Storage & PDF Processing)
# ---------------------------------------------------------------------------
import json
import logging

import boto3
from botocore.exceptions import ClientError

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_s3_client():
    """
    Creates a Boto3 client connected to our local MinIO instance.
    """
    return boto3.client(
        "s3",
        # 1. Point to MinIO (localhost:9000), not real AWS S3
        endpoint_url=settings.MINIO_ENDPOINT,
        # 2. Authentication Credentials (from .env)
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        # 3. Force S3v4 Signature
        # MinIO requires this modern signature version.
        # Without it, connection attempts might fail with 403 Forbidden.
        config=boto3.session.Config(signature_version="s3v4"),
    )


def init_storage():
    """
    Run on Startup: Ensures the storage backend is ready.
    1. Creates the 'resumes' Bucket if it doesn't exist.
    2. Sets the Bucket Policy to PUBLIC READ (Critical for the frontend).
    """
    s3 = get_s3_client()
    bucket_name = settings.MINIO_BUCKET_NAME

    # --- STEP 1: CREATE BUCKET ---
    try:
        # Check if bucket exists (Head Request is cheap/fast)
        s3.head_bucket(Bucket=bucket_name)
        logger.info(f"Storage: Bucket '{bucket_name}' exists.")
    except ClientError:
        try:
            # If not found, create it
            s3.create_bucket(Bucket=bucket_name)
            logger.info(f"Storage: Created bucket '{bucket_name}'.")
        except Exception as e:
            logger.error(f"Failed to create bucket: {e}")
            return

    # --- STEP 2: SET PUBLIC POLICY ---
    # By default, MinIO buckets are "Private Vaults".
    # If a Recruiter clicks "Download Resume" in the dashboard, the browser
    # tries to fetch the file directly. If the bucket is private, they get "Access Denied".

    # We define a policy that turns this specific bucket into a "Public Library".
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicRead",
                "Effect": "Allow",
                "Principal": "*",  # "*" means ANYONE (Public)
                "Action": ["s3:GetObject"],  # Allow Downloading/Reading
                "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
            }
        ],
    }

    try:
        s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
        logger.info("Storage: Bucket policy set to Public Read.")
    except Exception as e:
        logger.error(f"Failed to set bucket policy: {e}")

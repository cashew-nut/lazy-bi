"""Shared boto3 S3 client factory (emulator or real endpoint)."""
from __future__ import annotations

import boto3

from . import config


def client():
    access_key, secret_key, session_token = config.resolve_credentials()
    return boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        region_name=config.AWS_REGION,
    )

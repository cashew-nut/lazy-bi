"""Shared boto3 S3 client factory (emulator or real endpoint)."""
from __future__ import annotations

import boto3

from . import config


def client():
    return boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )

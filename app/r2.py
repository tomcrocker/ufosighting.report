import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterator

import boto3
from botocore.config import Config

from app.config import get_settings

ALLOWED_IMAGE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ALLOWED_VIDEO = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


@lru_cache
def client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.r2_endpoint,
        aws_access_key_id=s.r2_access_key,
        aws_secret_access_key=s.r2_secret_key,
        config=Config(
            signature_version="s3v4",
            region_name="auto",
            s3={"addressing_style": "path"},
        ),
    )


def make_upload_key(content_type: str) -> str:
    ext = (ALLOWED_IMAGE | ALLOWED_VIDEO)[content_type]
    now = datetime.now(timezone.utc)
    return f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"


def presign_put(key: str, content_type: str, size_bytes: int, expires: int = 900) -> str:
    s = get_settings()
    return client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": s.r2_bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size_bytes,
        },
        ExpiresIn=expires,
    )


def public_url(key: str) -> str:
    return f"{get_settings().media_base_url}/{key}"


def head_exists(key: str) -> bool:
    try:
        client().head_object(Bucket=get_settings().r2_bucket, Key=key)
        return True
    except client().exceptions.ClientError:
        return False


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    client().put_object(
        Bucket=get_settings().r2_bucket, Key=key, Body=data, ContentType=content_type
    )


def delete_key(key: str) -> None:
    client().delete_object(Bucket=get_settings().r2_bucket, Key=key)


def list_keys(prefix: str) -> Iterator[tuple[str, datetime]]:
    paginator = client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=get_settings().r2_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"], obj["LastModified"]

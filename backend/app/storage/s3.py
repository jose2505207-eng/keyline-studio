"""S3-compatible storage backend (AWS S3, MinIO, R2, ...) via boto3.

Credentials never reach the frontend: the browser only ever sees
short-lived presigned PUT/GET URLs.
"""

from __future__ import annotations

from .base import PresignedUpload, StorageError


class S3Storage:
    name = "s3"

    def __init__(self, bucket: str, endpoint_url: str = "", region: str = "",
                 access_key: str = "", secret_key: str = "",
                 secure: bool = True, public_endpoint_url: str = ""):
        import boto3
        from botocore.config import Config

        self.bucket = bucket

        def _client(endpoint: str):
            kwargs: dict = {
                "config": Config(signature_version="s3v4",
                                 s3={"addressing_style": "path"}),
            }
            if endpoint:
                if not endpoint.startswith(("http://", "https://")):
                    endpoint = ("https://" if secure else "http://") + endpoint
                kwargs["endpoint_url"] = endpoint
            if region:
                kwargs["region_name"] = region
            if access_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
            return boto3.client("s3", **kwargs)

        self.client = _client(endpoint_url)
        # Presigned URLs must be reachable from the *browser*; inside docker
        # the internal endpoint (http://minio:9000) is not.
        self.presign_client = (_client(public_endpoint_url)
                               if public_endpoint_url else self.client)

    def presign_put(self, key: str, content_type: str,
                    expiry_seconds: int) -> PresignedUpload:
        url = self.presign_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key,
                    "ContentType": content_type},
            ExpiresIn=expiry_seconds,
        )
        return PresignedUpload(key=key, url=url,
                               headers={"Content-Type": content_type})

    def _head(self, key: str):
        from botocore.exceptions import ClientError

        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError(f"S3 head failed for {key}: {code}") from exc

    def exists(self, key: str) -> bool:
        return self._head(key) is not None

    def size(self, key: str) -> int:
        head = self._head(key)
        if head is None:
            raise StorageError(f"Object not found: {key}")
        return int(head["ContentLength"])

    def download_to(self, key: str, dest_path: str) -> None:
        import os

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        self.client.download_file(self.bucket, key, dest_path)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType=content_type)

    def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                self.client.delete_objects(Bucket=self.bucket,
                                           Delete={"Objects": objs})
                deleted += len(objs)
        return deleted

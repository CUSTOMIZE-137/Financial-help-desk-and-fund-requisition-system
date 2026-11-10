"""Shared filesystem and S3-compatible object storage backends for evidence and signatures."""

import io
import os
from pathlib import Path
from urllib.parse import urljoin

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile, File
from django.core.files.storage import FileSystemStorage, Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class SharedFileSystemStorage(FileSystemStorage):
    """Multi-worker shared volume storage with group-writable permissions for NFS/EFS mounts."""

    def __init__(self, location=None, base_url=None, file_permissions_mode=0o664, directory_permissions_mode=0o775):
        super().__init__(
            location=location or settings.MEDIA_ROOT,
            base_url=base_url or settings.MEDIA_URL,
            file_permissions_mode=file_permissions_mode,
            directory_permissions_mode=directory_permissions_mode,
        )


@deconstructible
class S3CompatibleStorage(Storage):
    """S3-compatible object storage backend supporting AWS S3, MinIO, R2, and Spaces.

    Uses ``boto3`` (or ``django-storages``) under the hood while keeping objects private
    and generating time-limited presigned URLs only when requested by authorized views.
    """

    def __init__(
        self,
        bucket_name=None,
        region_name=None,
        endpoint_url=None,
        access_key=None,
        secret_key=None,
        key_prefix="",
        querystring_expire=300,
    ):
        self.bucket_name = bucket_name or getattr(settings, "AWS_STORAGE_BUCKET_NAME", "")
        self.region_name = region_name or getattr(settings, "AWS_S3_REGION_NAME", "us-east-1")
        self.endpoint_url = endpoint_url or getattr(settings, "AWS_S3_ENDPOINT_URL", None) or None
        self.access_key = access_key or getattr(settings, "AWS_ACCESS_KEY_ID", None) or None
        self.secret_key = secret_key or getattr(settings, "AWS_SECRET_ACCESS_KEY", None) or None
        self.key_prefix = (
            key_prefix if key_prefix is not None else getattr(settings, "AWS_LOCATION", "")
        ).strip("/")
        self.querystring_expire = int(
            querystring_expire or getattr(settings, "AWS_QUERYSTRING_EXPIRE", 300)
        )
        self._client = None

    def _normalize_key(self, name: str) -> str:
        """Convert OS-specific path separators to POSIX object keys under the configured prefix."""
        clean = str(name).replace("\\", "/").lstrip("/")
        if self.key_prefix:
            return f"{self.key_prefix}/{clean}"
        return clean

    @property
    def client(self):
        """Lazily initialize the boto3 S3 client so local dev/tests do not require AWS credentials."""
        if self._client is None:
            if not self.bucket_name:
                raise ImproperlyConfigured(
                    "AWS_STORAGE_BUCKET_NAME must be configured when using S3CompatibleStorage."
                )
            try:
                import boto3
            except ImportError as exc:
                raise ImproperlyConfigured(
                    "Install 'boto3' to use S3CompatibleStorage in production."
                ) from exc
            self._client = boto3.client(
                "s3",
                region_name=self.region_name,
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        return self._client

    def _open(self, name, mode="rb"):
        key = self._normalize_key(name)
        response = self.client.get_object(Bucket=self.bucket_name, Key=key)
        data = response["Body"].read()
        return ContentFile(data, name=name)

    def _save(self, name, content: File):
        clean_name = str(name).replace("\\", "/").lstrip("/")
        key = self._normalize_key(clean_name)
        if hasattr(content, "seek"):
            content.seek(0)
        body = content.read()
        if isinstance(body, str):
            body = body.encode("utf-8")
        extra_args = {"ServerSideEncryption": "AES256"} if getattr(settings, "AWS_S3_ENCRYPT", False) else {}
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=io.BytesIO(body),
            **extra_args,
        )
        return clean_name

    def exists(self, name) -> bool:
        key = self._normalize_key(name)
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except Exception:
            return False

    def delete(self, name):
        key = self._normalize_key(name)
        self.client.delete_object(Bucket=self.bucket_name, Key=key)

    def size(self, name) -> int:
        key = self._normalize_key(name)
        response = self.client.head_object(Bucket=self.bucket_name, Key=key)
        return int(response.get("ContentLength", 0))

    def url(self, name) -> str:
        key = self._normalize_key(name)
        custom_domain = getattr(settings, "AWS_S3_CUSTOM_DOMAIN", "")
        if custom_domain and not getattr(settings, "AWS_QUERYSTRING_AUTH", True):
            return urljoin(f"https://{custom_domain.rstrip('/')}/", key)
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": key},
            ExpiresIn=self.querystring_expire,
        )


def build_storages_setting(debug: bool = True) -> dict:
    """Build Django's STORAGES dictionary based on environment configuration."""
    backend_choice = os.getenv("STORAGE_BACKEND", "").strip().lower()
    s3_bucket = os.getenv("AWS_STORAGE_BUCKET_NAME", os.getenv("S3_BUCKET_NAME", "")).strip()

    if backend_choice == "s3" or (not backend_choice and s3_bucket):
        default_backend = "financial_helpdesk.storage.S3CompatibleStorage"
    elif backend_choice in ("shared", "shared_fs", "nfs"):
        default_backend = "financial_helpdesk.storage.SharedFileSystemStorage"
    else:
        default_backend = "django.core.files.storage.FileSystemStorage"

    staticfiles_backend = os.getenv(
        "STATICFILES_STORAGE_BACKEND",
        "django.contrib.staticfiles.storage.StaticFilesStorage"
        if debug
        else "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    )

    return {
        "default": {"BACKEND": default_backend},
        "staticfiles": {"BACKEND": staticfiles_backend},
    }

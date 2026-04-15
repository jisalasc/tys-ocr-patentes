import os
from datetime import timedelta

import google.auth
from google.api_core.exceptions import NotFound
from google.auth.transport.requests import Request
from google.cloud import storage


def get_bucket() -> storage.Bucket:
    bucket_name = os.getenv("GCS_BUCKET", "").strip()
    if not bucket_name:
        raise RuntimeError("Falta configurar la variable de entorno GCS_BUCKET.")

    client = storage.Client()
    return client.bucket(bucket_name)


def _get_signed_url_credentials() -> tuple[google.auth.credentials.Credentials, str]:
    credentials, _ = google.auth.default()
    auth_request = Request()

    if not getattr(credentials, "valid", False) or not getattr(credentials, "token", None):
        credentials.refresh(auth_request)

    service_account_email = getattr(credentials, "service_account_email", "")
    if not service_account_email:
        raise RuntimeError(
            "No fue posible resolver service_account_email para firmar URLs. "
            "En Cloud Run, usa una Service Account asociada al servicio."
        )

    return credentials, service_account_email


def generate_upload_url(filename: str, content_type: str, expires_minutes: int = 15) -> str:
    if not filename:
        raise ValueError("filename es requerido")
    if not content_type:
        raise ValueError("content_type es requerido")

    bucket = get_bucket()
    blob = bucket.blob(filename)
    credentials, service_account_email = _get_signed_url_credentials()

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expires_minutes),
        method="PUT",
        content_type=content_type,
        credentials=credentials,
        service_account_email=service_account_email,
        access_token=credentials.token,
    )


def generate_download_url(blob_name: str, expires_minutes: int = 60) -> str:
    if not blob_name:
        raise ValueError("blob_name es requerido")

    bucket = get_bucket()
    blob = bucket.blob(blob_name)
    credentials, service_account_email = _get_signed_url_credentials()

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expires_minutes),
        method="GET",
        credentials=credentials,
        service_account_email=service_account_email,
        access_token=credentials.token,
    )


def read_blob(blob_name: str) -> bytes:
    if not blob_name:
        raise ValueError("blob_name es requerido")

    bucket = get_bucket()
    blob = bucket.blob(blob_name)

    try:
        return blob.download_as_bytes()
    except NotFound as exc:
        raise FileNotFoundError(f"No existe el objeto en GCS: {blob_name}") from exc


def write_blob(blob_name: str, data: bytes, content_type: str = "application/zip") -> None:
    if not blob_name:
        raise ValueError("blob_name es requerido")

    bucket = get_bucket()
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)


def delete_blob(blob_name: str) -> None:
    if not blob_name:
        raise ValueError("blob_name es requerido")

    bucket = get_bucket()
    blob = bucket.blob(blob_name)

    try:
        blob.delete()
    except NotFound:
        return
"""
Загрузка готовых PDF в S3-совместимое хранилище (MinIO/AWS S3).

boto3 синхронный — вызываем его через asyncio.to_thread, чтобы не блокировать
event loop ARQ-воркера. Если S3 не настроен (нет s3_endpoint_url в settings),
upload_pdf возвращает None и воркер просто отправляет PDF в Telegram без
сохранения — это тот же паттерн "опционально, без Redis работаем без кеша",
что уже используется в generation/image_fetcher.py.
"""

import logging
from datetime import datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from config import settings

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
    return _client


def _put_object(key: str, body: bytes) -> None:
    client = _get_client()
    client.put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=body,
        ContentType="application/pdf",
    )


async def upload_pdf(job_id: str, pdf_bytes: bytes) -> str | None:
    """
    Загружает PDF в S3/MinIO. Возвращает object key, либо None если
    S3 не настроен или загрузка не удалась (лог warning, воркер продолжает
    без сохранения — отправка PDF в Telegram не должна зависеть от S3).
    """
    if not settings.s3_endpoint_url:
        logger.info("S3 not configured, skipping upload", extra={"job_id": job_id})
        return None

    date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
    key = f"presentations/{date_prefix}/{job_id}.pdf"

    import asyncio

    try:
        await asyncio.to_thread(_put_object, key, pdf_bytes)
    except (BotoCoreError, ClientError) as e:
        logger.warning("S3 upload failed", extra={"job_id": job_id, "error": str(e)})
        return None

    logger.info("PDF uploaded to S3", extra={"job_id": job_id, "key": key})
    return key

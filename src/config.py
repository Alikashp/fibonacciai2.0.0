from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    openai_api_key: str
    openai_base_url: str = "https://api.proxyapi.ru/openai/v1"
    openai_model: str = "gpt-4o"
    watermark_free_tier: bool = True

    # ── ARQ / очередь генерации ──────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── S3 / MinIO — хранилище готовых PDF ──────────────────────────────────
    s3_endpoint_url: str = ""       # пусто = MinIO/S3 не настроен, worker пропустит загрузку
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "fibonacci-presentations"
    s3_region: str = "us-east-1"

    class Config:
        env_file = ".env"


settings = Settings()

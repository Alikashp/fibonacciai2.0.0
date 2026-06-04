from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    openai_api_key: str
    openai_base_url: str = "https://api.proxyapi.ru/openai/v1"
    openai_model: str = "gpt-4o"
    watermark_free_tier: bool = True

    class Config:
        env_file = ".env"


settings = Settings()

"""
Async SQLAlchemy session factory + репозиторий пользователей.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Base, User, Presentation, PlanType

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

# Railway даёт postgres://, SQLAlchemy нужен postgresql+asyncpg://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = None
SessionLocal = None


async def init_db() -> None:
    """Инициализация БД при старте приложения."""
    global engine, SessionLocal

    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — работаем без БД")
        return

    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=5,
        max_overflow=10,
    )

    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database initialized")


async def close_db() -> None:
    if engine:
        await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    if SessionLocal is None:
        yield None
        return
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Репозиторий ────────────────────────────────────────────────────────────────

async def get_or_create_user(
    session: AsyncSession,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    language_code: str | None = None,
) -> User:
    """Возвращает пользователя или создаёт нового."""
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            user_id=user_id,
            username=username,
            first_name=first_name,
            language_code=language_code,
        )
        session.add(user)
        await session.flush()
        logger.info(f"New user: {user_id} @{username}")
    else:
        # Обновляем имя если изменилось
        if username and user.username != username:
            user.username = username
        if first_name and user.first_name != first_name:
            user.first_name = first_name
    return user


async def record_presentation(
    session: AsyncSession,
    user: User,
    topic: str,
    presentation_type: str,
    audience: str,
    language: str,
    slide_count: int | None = None,
    has_brief: bool = False,
    watermark: bool = True,
) -> Presentation:
    """Записывает презентацию и увеличивает счётчик пользователя."""
    presentation = Presentation(
        user_id=user.user_id,
        topic=topic,
        presentation_type=presentation_type,
        audience=audience,
        language=language,
        slide_count=slide_count,
        has_brief=has_brief,
        watermark=watermark,
    )
    session.add(presentation)
    user.presentations_count += 1
    await session.flush()
    return presentation


async def upgrade_user_plan(
    session: AsyncSession,
    user: User,
    plan: PlanType,
    telegram_payment_charge_id: str,
    stars_amount: int,
) -> None:
    """Обновляет план после оплаты."""
    from db.models import Payment
    user.plan = plan
    payment = Payment(
        user_id=user.user_id,
        telegram_payment_charge_id=telegram_payment_charge_id,
        plan=plan,
        stars_amount=stars_amount,
    )
    session.add(payment)
    await session.flush()
    logger.info(f"User {user.user_id} upgraded to {plan}, {stars_amount} stars")

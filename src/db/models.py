"""
Модели БД. SQLAlchemy async.

Таблицы:
- users       — все кто писал боту
- presentations — каждая сгенерированная презентация
- payments    — платежи через Telegram Stars

Решения:
- Без Alembic на старте — create_all при запуске. Добавим миграции когда схема устаканится.
- user_id = Telegram user id (int64) — естественный PK, не суррогатный
- presentations хранят тему и тип — для статистики и анализа
"""

from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Integer,
    String, Text, ForeignKey, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
import enum


class Base(DeclarativeBase):
    pass


class PlanType(str, enum.Enum):
    FREE    = "free"
    STARTER = "starter"
    PRO     = "pro"
    TEAM    = "team"


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))
    plan: Mapped[PlanType] = mapped_column(
        Enum(PlanType), default=PlanType.FREE, server_default="free"
    )
    presentations_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    presentations: Mapped[list["Presentation"]] = relationship(back_populates="user")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user")

    @property
    def free_limit(self) -> int:
        return 2

    @property
    def can_generate(self) -> bool:
        if self.plan != PlanType.FREE:
            return True
        return self.presentations_count < self.free_limit

    @property
    def presentations_left(self) -> int:
        if self.plan != PlanType.FREE:
            return 999
        return max(0, self.free_limit - self.presentations_count)


class Presentation(Base):
    __tablename__ = "presentations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id"))
    topic: Mapped[str] = mapped_column(Text)
    presentation_type: Mapped[str] = mapped_column(String(32))
    audience: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(8))
    slide_count: Mapped[int | None] = mapped_column(Integer)
    has_brief: Mapped[bool] = mapped_column(Boolean, default=False)
    watermark: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="presentations")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id"))
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(256), unique=True)
    plan: Mapped[PlanType] = mapped_column(Enum(PlanType))
    stars_amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="payments")

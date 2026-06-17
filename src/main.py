import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, LabeledPrice, PreCheckoutQuery,
)

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from schemas.presentation import UserRequest, PresentationType, AudienceType
from generation.llm import generate_presentation_structure
from generation.template_engine import render_presentation
from generation.pdf_renderer import html_to_pdf
from db.session import init_db, close_db, get_session, get_or_create_user, record_presentation, upgrade_user_plan
from db.models import PlanType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher(storage=MemoryStorage())

# ── Цены в Telegram Stars ─────────────────────────────────────────────────────
PLANS = {
    "starter": {"stars": 400,  "label": "Starter — 400 ⭐",  "plan": PlanType.STARTER},
    "pro":     {"stars": 800,  "label": "Pro — 800 ⭐",      "plan": PlanType.PRO},
}
# ~$0.013 за звезду → Starter ≈ $5, Pro ≈ $10 (Railway валюта, не доллары)


# ── FSM ───────────────────────────────────────────────────────────────────────

class Gen(StatesGroup):
    choosing_type     = State()
    entering_topic    = State()
    choosing_audience = State()
    choosing_language = State()
    entering_brief    = State()


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def kb_types() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Питч-дек",     callback_data="type:pitch_deck"),
            InlineKeyboardButton(text="🎓 Диплом",       callback_data="type:diploma"),
        ],
        [
            InlineKeyboardButton(text="📊 Отчёт",        callback_data="type:corp_report"),
            InlineKeyboardButton(text="📚 Обучающая",    callback_data="type:educational"),
        ],
        [
            InlineKeyboardButton(text="💼 Продажи",      callback_data="type:sales"),
            InlineKeyboardButton(text="🎤 Конференция",  callback_data="type:conference"),
        ],
        [
            InlineKeyboardButton(text="🗺 Роадмап",      callback_data="type:roadmap"),
        ],
    ])


def kb_audience() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Инвесторы",   callback_data="aud:investors"),
            InlineKeyboardButton(text="🤝 Клиенты",     callback_data="aud:clients"),
        ],
        [
            InlineKeyboardButton(text="👔 Руководство", callback_data="aud:management"),
            InlineKeyboardButton(text="👥 Коллеги",     callback_data="aud:colleagues"),
        ],
        [
            InlineKeyboardButton(text="🎓 Студенты",    callback_data="aud:students"),
            InlineKeyboardButton(text="🌍 Все",         callback_data="aud:general"),
        ],
    ])


def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский",    callback_data="lang:ru"),
            InlineKeyboardButton(text="🇬🇧 English",    callback_data="lang:en"),
        ],
        [
            InlineKeyboardButton(text="🇺🇿 O'zbek",     callback_data="lang:uz"),
            InlineKeyboardButton(text="🇰🇿 Қазақша",    callback_data="lang:kk"),
        ],
    ])


def kb_brief() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ Пропустить", callback_data="brief:skip"),
        ],
    ])


def kb_after_pdf() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Новая презентация", callback_data="action:new"),
        ],
    ])


def kb_paywall() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Starter — 400 звёзд", callback_data="pay:starter")],
        [InlineKeyboardButton(text="⭐ Pro — 800 звёзд",     callback_data="pay:pro")],
    ])


# ── Лейблы ────────────────────────────────────────────────────────────────────

TYPE_LABELS = {
    "pitch_deck":  "Питч-дек",
    "diploma":     "Диплом",
    "corp_report": "Корпоративный отчёт",
    "educational": "Обучающая",
    "sales":       "Продажная",
    "conference":  "Конференция",
    "roadmap":     "Роадмап",
}

AUDIENCE_LABELS = {
    "investors":  "Инвесторы",
    "clients":    "Клиенты",
    "management": "Руководство",
    "colleagues": "Коллеги",
    "students":   "Студенты",
    "general":    "Все",
}

BRIEF_HINTS = {
    "pitch_deck": (
        "— Команда: имена, роли, опыт\n"
        "— Тракшн: пользователи, выручка, рост\n"
        "— Инвестиции: сколько ищете\n"
        "— Контакты: email, telegram"
    ),
    "diploma": (
        "— Научный руководитель\n"
        "— Объект исследования\n"
        "— Методы и результаты"
    ),
    "corp_report": (
        "— Период отчёта\n"
        "— Ключевые метрики\n"
        "— Проблемы и планы"
    ),
    "sales": (
        "— Продукт и цена\n"
        "— Кейсы клиентов\n"
        "— Контакты"
    ),
}

DEFAULT_BRIEF_HINT = (
    "— команда и опыт\n"
    "— метрики и результаты\n"
    "— контакты"
)


# ── Handlers ──────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    # Регистрируем пользователя
    async with get_session() as session:
        if session:
            user = await get_or_create_user(
                session,
                user_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                language_code=message.from_user.language_code,
            )
            left = user.presentations_left
            if user.plan != "free":
                plan_info = f"\n\n💎 План: {user.plan} · Презентации: ∞"
            else:
                plan_info = f"\n\n🎁 Бесплатно осталось: {left} из 2"
        else:
            plan_info = ""

    await message.answer(
        f"👋 Привет! Я <b>Fibonacci AI</b> — создаю профессиональные презентации за 60 секунд.{plan_info}\n\n"
        "Выберите тип презентации:",
        parse_mode="HTML",
        reply_markup=kb_types(),
    )
    await state.set_state(Gen.choosing_type)


@dp.message(Command("plan"))
async def cmd_plan(message: Message):
    async with get_session() as session:
        if not session:
            await message.answer("База данных недоступна.")
            return
        user = await get_or_create_user(session, message.from_user.id)
        if user.plan == "free":
            text = (
                f"📊 Ваш план: <b>Free</b>\n"
                f"Использовано: {user.presentations_count} из 2\n\n"
                f"Для продолжения работы оформите подписку:"
            )
            await message.answer(text, parse_mode="HTML", reply_markup=kb_paywall())
        else:
            await message.answer(
                f"📊 Ваш план: <b>{user.plan.value.title()}</b>\n"
                f"Всего сгенерировано: {user.presentations_count} презентаций",
                parse_mode="HTML",
            )


@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Выберите тип презентации:", reply_markup=kb_types())
    await state.set_state(Gen.choosing_type)


@dp.callback_query(F.data.startswith("type:"))
async def on_type(call: CallbackQuery, state: FSMContext):
    ptype = call.data.split(":")[1]
    await state.update_data(presentation_type=ptype)
    try:
        await call.message.edit_text(
            f"Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n\n"
            "✏️ Напишите тему презентации.\n"
            "<i>Например: «Стартап по доставке еды для собак»</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await state.set_state(Gen.entering_topic)
    await call.answer()


@dp.message(Gen.entering_topic)
async def on_topic(message: Message, state: FSMContext):
    topic = message.text.strip()
    if len(topic) < 3:
        await message.answer("Тема слишком короткая.")
        return
    if len(topic) > 300:
        await message.answer("Тема слишком длинная. Сократите до 300 символов.")
        return
    await state.update_data(topic=topic)
    data = await state.get_data()
    ptype = data.get("presentation_type", "pitch_deck")
    await message.answer(
        f"Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n"
        f"Тема: <b>{topic[:60]}{'...' if len(topic) > 60 else ''}</b>\n\n"
        "Кто будет смотреть презентацию?",
        parse_mode="HTML",
        reply_markup=kb_audience(),
    )
    await state.set_state(Gen.choosing_audience)


@dp.callback_query(F.data.startswith("aud:"))
async def on_audience(call: CallbackQuery, state: FSMContext):
    audience = call.data.split(":")[1]
    await state.update_data(audience=audience)
    data = await state.get_data()
    if not data.get("presentation_type"):
        await call.answer()
        await state.clear()
        await call.message.answer("Сессия устарела. Начнём заново:", reply_markup=kb_types())
        await state.set_state(Gen.choosing_type)
        return
    try:
        await call.message.edit_text(
            "На каком языке делаем презентацию?",
            reply_markup=kb_language(),
        )
    except Exception:
        pass
    await state.set_state(Gen.choosing_language)
    await call.answer()


@dp.callback_query(F.data.startswith("lang:"))
async def on_language(call: CallbackQuery, state: FSMContext):
    lang = call.data.split(":")[1]
    await state.update_data(language=lang)
    data = await state.get_data()
    ptype = data.get("presentation_type")
    if not ptype:
        await call.answer()
        await state.clear()
        await call.message.answer("Сессия устарела. Начнём заново:", reply_markup=kb_types())
        await state.set_state(Gen.choosing_type)
        return
    hint = BRIEF_HINTS.get(ptype, DEFAULT_BRIEF_HINT)
    try:
        await call.message.edit_text(
            "📝 <b>Расскажите о проекте</b> — необязательно, но улучшит результат.\n\n"
            f"<i>{hint}</i>\n\n"
            "Или нажмите кнопку ниже чтобы пропустить.",
            parse_mode="HTML",
            reply_markup=kb_brief(),
        )
    except Exception:
        pass
    await state.set_state(Gen.entering_brief)
    await call.answer()


@dp.message(Gen.entering_brief)
async def on_brief_text(message: Message, state: FSMContext):
    brief = message.text.strip()
    if len(brief) > 2000:
        await message.answer("Слишком длинный бриф. Сократите до 2000 символов.")
        return
    await state.update_data(brief=brief)
    data = await state.get_data()
    await _confirm_and_generate(message, data, state)


@dp.callback_query(F.data == "brief:skip")
async def on_brief_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(brief=None)
    data = await state.get_data()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer()
    await _confirm_and_generate(call.message, data, state)


@dp.callback_query(F.data == "action:new")
async def on_new(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Выберите тип презентации:", reply_markup=kb_types())
    await state.set_state(Gen.choosing_type)
    await call.answer()


# ── Оплата через Telegram Stars ───────────────────────────────────────────────

@dp.callback_query(F.data.startswith("pay:"))
async def on_pay(call: CallbackQuery):
    plan_key = call.data.split(":")[1]
    plan = PLANS.get(plan_key)
    if not plan:
        await call.answer("Неизвестный тариф")
        return

    plan_descriptions = {
        "starter": "15 презентаций · Все шаблоны · Без водяного знака",
        "pro": "50 презентаций · Всё из Starter · AI-изображения · Приоритетная очередь",
    }

    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"Fibonacci AI — {plan_key.title()}",
        description=plan_descriptions.get(plan_key, ""),
        payload=f"plan:{plan_key}",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label=plan["label"], amount=plan["stars"])],
    )
    await call.answer()


@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload  # "plan:starter" или "plan:pro"
    plan_key = payload.split(":")[1] if ":" in payload else "starter"
    plan_info = PLANS.get(plan_key, PLANS["starter"])

    async with get_session() as session:
        if session:
            user = await get_or_create_user(session, message.from_user.id)
            await upgrade_user_plan(
                session,
                user=user,
                plan=plan_info["plan"].value,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                stars_amount=payment.total_amount,
            )

    plan_limits = {"starter": "15", "pro": "50"}
    await message.answer(
        f"🎉 Оплата прошла! Добро пожаловать в <b>{plan_key.title()}</b>.\n\n"
        f"Доступно презентаций: <b>{plan_limits.get(plan_key, '∞')}</b>\n"
        f"Водяной знак: <b>убран</b>\n\n"
        f"Создайте первую презентацию: /new",
        parse_mode="HTML",
    )


# ── Подтверждение и запуск ────────────────────────────────────────────────────

async def _confirm_and_generate(message: Message, data: dict, state: FSMContext):
    ptype    = data["presentation_type"]
    topic    = data["topic"]
    audience = data["audience"]
    lang     = data["language"]
    brief    = data.get("brief")

    # Проверяем лимит
    async with get_session() as session:
        if session:
            user = await get_or_create_user(
                session,
                user_id=message.chat.id,
            )
            if not user.can_generate:
                await state.clear()
                await message.answer(
                    "😔 Вы использовали все бесплатные презентации (2 из 2).\n\n"
                    "Оформите подписку чтобы продолжить:",
                    reply_markup=kb_paywall(),
                )
                return
            watermark = user.plan == "free"
        else:
            watermark = True

    brief_line = "\n• <b>Бриф:</b> добавлен ✓" if brief else ""
    await message.answer(
        f"✅ <b>Создаю презентацию:</b>\n\n"
        f"• Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n"
        f"• Тема: <b>{topic[:60]}{'...' if len(topic) > 60 else ''}</b>\n"
        f"• Аудитория: <b>{AUDIENCE_LABELS.get(audience, audience)}</b>\n"
        f"• Язык: <b>{lang.upper()}</b>"
        f"{brief_line}\n\n"
        f"⏳ Обычно занимает 60–90 секунд.",
        parse_mode="HTML",
    )
    await state.clear()
    await generate_and_send(message, data, watermark=watermark)


# ── Генерация ─────────────────────────────────────────────────────────────────

async def generate_and_send(message: Message, data: dict, watermark: bool = True):
    status_msg = await message.answer("⚙️ Генерирую структуру слайдов...")

    try:
        brief = data.get("brief")
        extra = None
        if brief:
            extra = (
                f"ВАЖНО — используй эти реальные данные:\n\n{brief}\n\n"
                f"Вставляй точно: имена, цифры, контакты."
            )

        request = UserRequest(
            topic=data["topic"],
            presentation_type=PresentationType(data["presentation_type"]),
            audience=AudienceType(data["audience"]),
            language=data["language"],
            extra_instructions=extra,
        )

        await status_msg.edit_text("⚙️ Пишу текст слайдов...")
        presentation = await generate_presentation_structure(request)

        await status_msg.edit_text("⚙️ Подбираю изображения...")
        from generation.image_fetcher import fetch_images_for_slides
        image_urls = await fetch_images_for_slides(presentation.slides)

        await status_msg.edit_text("⚙️ Собираю дизайн...")
        html = render_presentation(presentation, image_urls=image_urls, watermark=watermark)

        await status_msg.edit_text("⚙️ Рендерю PDF...")
        has_mermaid = any(s.layout.value == "diagram" for s in presentation.slides)
        pdf_bytes = await html_to_pdf(html, has_mermaid=has_mermaid)

        # Записываем в БД
        async with get_session() as session:
            if session:
                user = await get_or_create_user(session, message.chat.id)
                await record_presentation(
                    session,
                    user=user,
                    topic=data["topic"],
                    presentation_type=data["presentation_type"],
                    audience=data["audience"],
                    language=data["language"],
                    slide_count=presentation.slide_count,
                    has_brief=bool(data.get("brief")),
                    watermark=watermark,
                )

        await status_msg.delete()

        safe_title = "".join(
            c if c.isalnum() or c in " _-" else "_"
            for c in presentation.meta.title
        )[:40]

        caption = (
            f"✨ <b>{presentation.meta.title}</b>\n"
            f"{presentation.slide_count} слайдов · {TYPE_LABELS.get(data['presentation_type'], '')}"
        )
        if watermark:
            caption += "\n\n<i>Бесплатная версия · Уберите водяной знак в /plan</i>"

        await message.answer_document(
            document=BufferedInputFile(pdf_bytes, filename=f"{safe_title}.pdf"),
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb_after_pdf(),
        )

    except Exception as e:
        logger.exception(f"Generation failed: {e}")
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуйте ещё раз через /new",
            parse_mode="HTML",
        )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    from generation.pdf_renderer import get_renderer, shutdown_renderer

    logger.info("Starting Fibonacci AI bot...")
    await init_db()
    await get_renderer()
    logger.info("Playwright ready")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "pre_checkout_query"],
        )
    finally:
        await shutdown_renderer()
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

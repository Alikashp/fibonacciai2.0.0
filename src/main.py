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
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from schemas.presentation import (
    UserRequest, PresentationType, AudienceType
)
from generation.llm import generate_presentation_structure
from generation.template_engine import render_presentation
from generation.pdf_renderer import html_to_pdf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher(storage=MemoryStorage())


# ── FSM состояния ──────────────────────────────────────────────────────────────

class Gen(StatesGroup):
    choosing_type     = State()
    entering_topic    = State()
    choosing_audience = State()
    choosing_language = State()


# ── Клавиатуры ─────────────────────────────────────────────────────────────────

def kb_types() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Питч-дек",      callback_data="type:pitch_deck"),
            InlineKeyboardButton(text="🎓 Диплом",        callback_data="type:diploma"),
        ],
        [
            InlineKeyboardButton(text="📊 Отчёт",         callback_data="type:corp_report"),
            InlineKeyboardButton(text="📚 Обучающая",     callback_data="type:educational"),
        ],
        [
            InlineKeyboardButton(text="💼 Продажи",       callback_data="type:sales"),
            InlineKeyboardButton(text="🎤 Конференция",   callback_data="type:conference"),
        ],
        [
            InlineKeyboardButton(text="🗺 Роадмап",       callback_data="type:roadmap"),
        ],
    ])


def kb_audience() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Инвесторы",    callback_data="aud:investors"),
            InlineKeyboardButton(text="🤝 Клиенты",      callback_data="aud:clients"),
        ],
        [
            InlineKeyboardButton(text="👔 Руководство",  callback_data="aud:management"),
            InlineKeyboardButton(text="👥 Коллеги",      callback_data="aud:colleagues"),
        ],
        [
            InlineKeyboardButton(text="🎓 Студенты",     callback_data="aud:students"),
            InlineKeyboardButton(text="🌍 Все",          callback_data="aud:general"),
        ],
    ])


def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский",     callback_data="lang:ru"),
            InlineKeyboardButton(text="🇬🇧 English",     callback_data="lang:en"),
        ],
        [
            InlineKeyboardButton(text="🇺🇿 O'zbek",      callback_data="lang:uz"),
            InlineKeyboardButton(text="🇰🇿 Қазақша",     callback_data="lang:kk"),
        ],
    ])


def kb_after_pdf() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Новая презентация", callback_data="action:new"),
        ],
    ])


# ── Человекочитаемые названия ──────────────────────────────────────────────────

TYPE_LABELS = {
    "pitch_deck":   "Питч-дек",
    "diploma":      "Диплом",
    "corp_report":  "Корпоративный отчёт",
    "educational":  "Обучающая",
    "sales":        "Продажная",
    "conference":   "Конференция",
    "roadmap":      "Роадмап",
}

AUDIENCE_LABELS = {
    "investors":    "Инвесторы",
    "clients":      "Клиенты",
    "management":   "Руководство",
    "colleagues":   "Коллеги",
    "students":     "Студенты",
    "general":      "Все",
}


# ── Handlers ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я <b>Fibonacci AI</b> — создаю профессиональные презентации за 60 секунд.\n\n"
        "Выберите тип презентации:",
        parse_mode="HTML",
        reply_markup=kb_types(),
    )
    await state.set_state(Gen.choosing_type)


@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Выберите тип презентации:",
        reply_markup=kb_types(),
    )
    await state.set_state(Gen.choosing_type)


@dp.callback_query(F.data.startswith("type:"))
async def on_type(call: CallbackQuery, state: FSMContext):
    ptype = call.data.split(":")[1]
    await state.update_data(presentation_type=ptype)
    await call.message.edit_text(
        f"Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n\n"
        "✏️ Напишите тему презентации.\n"
        "<i>Например: «Стартап по доставке еды для собак» или «Анализ рынка e-commerce в СНГ»</i>",
        parse_mode="HTML",
    )
    await state.set_state(Gen.entering_topic)
    await call.answer()


@dp.message(Gen.entering_topic)
async def on_topic(message: Message, state: FSMContext):
    topic = message.text.strip()
    if len(topic) < 3:
        await message.answer("Тема слишком короткая. Напишите подробнее.")
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
    await call.message.edit_text(
        "На каком языке делаем презентацию?",
        reply_markup=kb_language(),
    )
    await state.set_state(Gen.choosing_language)
    await call.answer()


@dp.callback_query(F.data.startswith("lang:"))
async def on_language(call: CallbackQuery, state: FSMContext):
    lang = call.data.split(":")[1]
    await state.update_data(language=lang)
    data = await state.get_data()

    ptype    = data["presentation_type"]
    topic    = data["topic"]
    audience = data["audience"]

    await call.message.edit_text(
        f"✅ Всё понял. Создаю:\n\n"
        f"• Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n"
        f"• Тема: <b>{topic[:60]}{'...' if len(topic) > 60 else ''}</b>\n"
        f"• Аудитория: <b>{AUDIENCE_LABELS.get(audience, audience)}</b>\n"
        f"• Язык: <b>{lang.upper()}</b>\n\n"
        f"⏳ Обычно занимает 60–90 секунд. Не закрывайте чат.",
        parse_mode="HTML",
    )
    await call.answer()

    # Запускаем генерацию
    await generate_and_send(call.message, data)
    await state.clear()


@dp.callback_query(F.data == "action:new")
async def on_new(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer(
        "Выберите тип презентации:",
        reply_markup=kb_types(),
    )
    await state.set_state(Gen.choosing_type)
    await call.answer()


# ── Генерация ──────────────────────────────────────────────────────────────────

async def generate_and_send(message: Message, data: dict):
    """
    Синхронная генерация без очереди — для MVP / первых 10 пользователей.
    Пользователь видит "генерирую..." и ждёт.
    TODO: заменить на ARQ-задачу когда появится нагрузка.
    """
    status_msg = await message.answer("⚙️ Генерирую структуру слайдов...")

    try:
        # 1. Формируем запрос
        request = UserRequest(
            topic=data["topic"],
            presentation_type=PresentationType(data["presentation_type"]),
            audience=AudienceType(data["audience"]),
            language=data["language"],
        )

        # 2. LLM → структура
        await status_msg.edit_text("⚙️ Пишу текст слайдов...")
        presentation = await generate_presentation_structure(request)

        # 3. Фото
        await status_msg.edit_text("⚙️ Подбираю изображения...")
        from generation.image_fetcher import fetch_images_for_slides
        image_urls = await fetch_images_for_slides(presentation.slides)

        # 4. HTML
        await status_msg.edit_text("⚙️ Собираю дизайн...")
        html = render_presentation(presentation, image_urls=image_urls, watermark=True)

        # 4. PDF
        await status_msg.edit_text("⚙️ Рендерю PDF...")
        has_mermaid = any(
            s.layout.value == "diagram" for s in presentation.slides
        )
        pdf_bytes = await html_to_pdf(html, has_mermaid=has_mermaid)

        # 5. Отправляем
        await status_msg.delete()

        safe_title = "".join(
            c if c.isalnum() or c in " _-" else "_"
            for c in presentation.meta.title
        )[:40]
        filename = f"{safe_title}.pdf"

        await message.answer_document(
            document=BufferedInputFile(pdf_bytes, filename=filename),
            caption=(
                f"✨ <b>{presentation.meta.title}</b>\n"
                f"{presentation.slide_count} слайдов · {TYPE_LABELS.get(data['presentation_type'], '')}\n\n"
                f"<i>Бесплатная версия содержит водяной знак</i>"
            ),
            parse_mode="HTML",
            reply_markup=kb_after_pdf(),
        )

    except Exception as e:
        logger.exception(f"Generation failed: {e}")
        await status_msg.edit_text(
            "❌ Что-то пошло не так при генерации. Попробуйте ещё раз через /new\n\n"
            f"<i>Если ошибка повторяется — напишите нам.</i>",
            parse_mode="HTML",
        )


# ── Запуск ─────────────────────────────────────────────────────────────────────

async def main():
    from generation.pdf_renderer import get_renderer, shutdown_renderer

    logger.info("Starting Fibonacci AI bot...")

    # Прогреваем Playwright заранее — не ждём первого пользователя
    await get_renderer()
    logger.info("Playwright ready")

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await shutdown_renderer()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

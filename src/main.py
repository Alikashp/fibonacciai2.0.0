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


# ── FSM ───────────────────────────────────────────────────────────────────────

class Gen(StatesGroup):
    choosing_type     = State()
    entering_topic    = State()
    choosing_audience = State()
    choosing_language = State()
    entering_brief    = State()  # Новый шаг — бриф


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
            InlineKeyboardButton(text="⚡ Пропустить — сгенерировать без брифа", callback_data="brief:skip"),
        ],
    ])


def kb_after_pdf() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Новая презентация", callback_data="action:new"),
        ],
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
        "Например:\n"
        "— Команда: Алия — CEO, 8 лет в финтехе. Марат — CTO, ex-Kaspi\n"
        "— Тракшн: 1200 пользователей, $15K MRR, рост 30%/мес\n"
        "— Инвестиции: ищем $300K, уже есть оффер от одного инвестора\n"
        "— Контакты: ali@startup.kz, @ali_ceo"
    ),
    "diploma": (
        "Например:\n"
        "— Научный руководитель: проф. Иванов И.И.\n"
        "— Объект исследования: рынок e-commerce в Казахстане 2020-2024\n"
        "— Методы: анкетирование 200 респондентов, регрессионный анализ\n"
        "— Основной результат: корреляция 0.87 между X и Y"
    ),
    "corp_report": (
        "Например:\n"
        "— Период: Q1 2025\n"
        "— Выручка: 45M тенге (+12% к плану)\n"
        "— Команда: 23 человека, открыты 3 вакансии\n"
        "— Проблемы: задержка поставщика на 2 недели\n"
        "— План Q2: запуск нового продукта, цель 55M"
    ),
    "sales": (
        "Например:\n"
        "— Продукт: CRM-система для малого бизнеса\n"
        "— Цена: от $49/мес, есть пробный период 14 дней\n"
        "— Кейс: клиент X увеличил продажи на 34% за 3 месяца\n"
        "— Контакт: sales@company.com, +7 777 123-45-67"
    ),
}

DEFAULT_BRIEF_HINT = (
    "Напишите любую информацию о проекте:\n"
    "— команда и опыт\n"
    "— текущие метрики и результаты\n"
    "— ключевые факты которые нужно включить\n"
    "— контакты для финального слайда"
)


# ── Handlers ──────────────────────────────────────────────────────────────────

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
    try:
        await call.message.edit_text(
            f"Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n\n"
            "✏️ Напишите тему презентации.\n"
            "<i>Например: «Стартап по доставке еды для собак» или «Анализ рынка e-commerce в СНГ»</i>",
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
    ptype = data["presentation_type"]

    hint = BRIEF_HINTS.get(ptype, DEFAULT_BRIEF_HINT)

    try:
        await call.message.edit_text(
            "📝 <b>Расскажите о проекте</b> — необязательно, но сильно улучшит результат.\n\n"
            "Добавьте реальные данные которые нужно включить:\n"
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
        await message.answer(
            "Слишком длинный бриф. Сократите до 2000 символов — оставьте самое важное."
        )
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
    await call.message.answer(
        "Выберите тип презентации:",
        reply_markup=kb_types(),
    )
    await state.set_state(Gen.choosing_type)
    await call.answer()


# ── Подтверждение и запуск ────────────────────────────────────────────────────

async def _confirm_and_generate(message: Message, data: dict, state: FSMContext):
    ptype    = data["presentation_type"]
    topic    = data["topic"]
    audience = data["audience"]
    lang     = data["language"]
    brief    = data.get("brief")

    brief_line = "\n• <b>Бриф:</b> добавлен ✓" if brief else ""

    await message.answer(
        f"✅ <b>Создаю презентацию:</b>\n\n"
        f"• Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n"
        f"• Тема: <b>{topic[:60]}{'...' if len(topic) > 60 else ''}</b>\n"
        f"• Аудитория: <b>{AUDIENCE_LABELS.get(audience, audience)}</b>\n"
        f"• Язык: <b>{lang.upper()}</b>"
        f"{brief_line}\n\n"
        f"⏳ Обычно занимает 60–90 секунд. Не закрывайте чат.",
        parse_mode="HTML",
    )
    await state.clear()
    await generate_and_send(message, data)


# ── Генерация ─────────────────────────────────────────────────────────────────

async def generate_and_send(message: Message, data: dict):
    status_msg = await message.answer("⚙️ Генерирую структуру слайдов...")

    try:
        brief = data.get("brief")

        # Если есть бриф — добавляем в extra_instructions
        extra = None
        if brief:
            extra = (
                f"ВАЖНО — используй эти реальные данные в презентации:\n\n"
                f"{brief}\n\n"
                f"Вставляй эти данные точно как написано: имена, цифры, контакты."
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

        await status_msg.edit_text("⚙️ Собираю дизайн...")
        html = render_presentation(presentation, watermark=True)

        await status_msg.edit_text("⚙️ Рендерю PDF...")
        has_mermaid = any(s.layout.value == "diagram" for s in presentation.slides)
        pdf_bytes = await html_to_pdf(html, has_mermaid=has_mermaid)

        await status_msg.delete()

        safe_title = "".join(
            c if c.isalnum() or c in " _-" else "_"
            for c in presentation.meta.title
        )[:40]

        await message.answer_document(
            document=BufferedInputFile(pdf_bytes, filename=f"{safe_title}.pdf"),
            caption=(
                f"✨ <b>{presentation.meta.title}</b>\n"
                f"{presentation.slide_count} слайдов · "
                f"{TYPE_LABELS.get(data['presentation_type'], '')}\n\n"
                f"<i>Бесплатная версия содержит водяной знак</i>"
            ),
            parse_mode="HTML",
            reply_markup=kb_after_pdf(),
        )

    except Exception as e:
        logger.exception(f"Generation failed: {e}")
        await status_msg.edit_text(
            "❌ Что-то пошло не так. Попробуйте ещё раз через /new\n\n"
            "<i>Если ошибка повторяется — напишите нам.</i>",
            parse_mode="HTML",
        )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    from generation.pdf_renderer import get_renderer, shutdown_renderer

    logger.info("Starting Fibonacci AI bot...")
    await get_renderer()
    logger.info("Playwright ready")

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await shutdown_renderer()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

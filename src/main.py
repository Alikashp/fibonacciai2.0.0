import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
)
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from schemas.presentation import PresentationType
from db.session import init_db, close_db, get_session, get_or_create_user, upgrade_user_plan
from db.models import PlanType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)

# ── ARQ pool — соединение с очередью Redis, к которой обращается worker.py ────
# Генерация (LLM → картинки → PDF) больше НЕ идёт в этом процессе: хендлер
# только кладёт job в очередь и сразу отвечает пользователю. Реальная работа —
# в worker.py (запускается отдельным процессом: `arq worker.WorkerSettings`).
arq_pool: ArqRedis | None = None
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
    choosing_scheme   = State()
    onboarding        = State()  # генерическое состояние для ONBOARDING_QUESTIONS
    entering_material = State()  # DOKLAD: пользователь шлёт текст/документ по теме


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def kb_types() -> InlineKeyboardMarkup:
    # Остальные типы (DIPLOMA, EDUCATIONAL, SALES, ROADMAP, CONFERENCE отдельным
    # пунктом) намеренно скрыты из выбора — код и enum-значения не трогаем,
    # они пригодятся, когда будем возвращать типы в меню по одному.
    # DOKLAD сейчас — единственная замена CONFERENCE в пользовательском флоу:
    # какой именно HTML-шаблон (conference/corp_report) получится, решает
    # source_type запроса, а не выбор пользователя здесь (см. template_engine).
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Питч-дек", callback_data="type:pitch_deck"),
            InlineKeyboardButton(text="🎤 Доклад",   callback_data="type:doklad"),
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


def kb_scheme(plan: str = "free") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⬜ Classic Light", callback_data="scheme:light")],
    ]
    if plan != "free":
        rows += [
            [InlineKeyboardButton(text="🌑 Midnight",      callback_data="scheme:dark")],
            [InlineKeyboardButton(text="🌿 Forest",        callback_data="scheme:forest")],
            [InlineKeyboardButton(text="🔥 Ember",         callback_data="scheme:ember")],
        ]
    else:
        rows += [
            [InlineKeyboardButton(text="🌑 Midnight  🔒",  callback_data="scheme:locked")],
            [InlineKeyboardButton(text="🌿 Forest  🔒",    callback_data="scheme:locked")],
            [InlineKeyboardButton(text="🔥 Ember  🔒",     callback_data="scheme:locked")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    "doklad":      "Доклад",
}

AUDIENCE_LABELS = {
    "investors":  "Инвесторы",
    "clients":    "Клиенты",
    "management": "Руководство",
    "colleagues": "Коллеги",
    "students":   "Студенты",
    "general":    "Все",
}

# ── Онбординг-вопросы по типу презентации ──────────────────────────────────────
# Раньше был один флоу "Расскажите о проекте" на все типы (с разным только
# текстом подсказки). Теперь у каждого PresentationType — свой список вопросов:
# PITCH_DECK получает исходный вопрос как есть, DOKLAD — свой, короче.

@dataclass
class OnboardingQuestion:
    key: str                              # ключ в FSM-данных (и дальше в UserRequest/job kwargs)
    prompt: str                           # текст вопроса (HTML)
    kind: str                             # "text" | "choice"
    choices: list[tuple[str, str]] | None = None  # [(label, value), ...] — только для kind="choice"
    skippable: bool = False               # только для kind="text"
    skip_label: str = "⚡ Пропустить"


ONBOARDING_QUESTIONS: dict[PresentationType, list[OnboardingQuestion]] = {

    PresentationType.PITCH_DECK: [
        OnboardingQuestion(
            key="brief",
            kind="text",
            prompt=(
                "📝 <b>Расскажите о проекте</b> — необязательно, но улучшит результат.\n\n"
                "<i>— Команда: имена, роли, опыт\n"
                "— Тракшн: пользователи, выручка, рост\n"
                "— Инвестиции: сколько ищете\n"
                "— Контакты: email, telegram</i>\n\n"
                "Или нажмите кнопку ниже чтобы пропустить."
            ),
            skippable=True,
        ),
    ],

    PresentationType.DOKLAD: [
        OnboardingQuestion(
            key="content_volume",
            kind="choice",
            prompt="📏 <b>Насколько подробным сделать доклад?</b>",
            choices=[("⚡ Кратко", "short"), ("📄 Стандартно", "medium"), ("📚 Подробно", "long")],
        ),
        OnboardingQuestion(
            key="has_material",
            kind="choice",
            prompt="📎 <b>У вас есть готовый текст или документ по теме?</b>\n\n<i>Если да — доклад соберём строго по вашему материалу, без выдумывания фактов.</i>",
            choices=[("✅ Да, есть", "yes"), ("🆕 Нет, с нуля по теме", "no")],
        ),
    ],

    # DIPLOMA / EDUCATIONAL / SALES / CONFERENCE / ROADMAP — намеренно НЕ
    # заполнены. Эти 5 типов сейчас недостижимы из kb_types() (см. Task A3 —
    # в меню выбора только "Питч-дек" и "Доклад"), поэтому вопросы под них
    # ещё не написаны. Заполнить, когда будем возвращать типы в меню по одному.
}


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
    if ptype == "doklad":
        topic_prompt = (
            "✏️ Напишите тему презентации.\n"
            "<i>Например: «Как работает нейросеть простыми словами» — "
            "или пришлите готовый текст/документ по теме на следующем шаге.</i>"
        )
    else:
        topic_prompt = (
            "✏️ Напишите тему презентации.\n"
            "<i>Например: «Стартап по доставке еды для собак»</i>"
        )
    try:
        await call.message.edit_text(
            f"Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n\n"
            f"{topic_prompt}",
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
    # Получаем план пользователя для показа схем
    user_plan = "free"
    async with get_session() as session:
        if session:
            user = await get_or_create_user(session, call.from_user.id)
            user_plan = user.plan

    try:
        await call.message.edit_text(
            "🎨 <b>Выберите цветовую схему</b>\n\n"
            "<i>Midnight, Forest и Ember доступны на платном плане</i>",
            parse_mode="HTML",
            reply_markup=kb_scheme(user_plan),
        )
    except Exception:
        pass
    await state.set_state(Gen.choosing_scheme)
    await call.answer()


@dp.callback_query(F.data.startswith("scheme:"))
async def on_scheme(call: CallbackQuery, state: FSMContext):
    scheme = call.data.split(":")[1]
    if scheme == "locked":
        await call.answer("🔒 Доступно на платном плане. Используйте /plan", show_alert=True)
        return
    await state.update_data(color_scheme=scheme, onboarding_index=0)
    data = await state.get_data()
    await call.answer()
    await _ask_onboarding_question(call.message, data, state)


# ── Онбординг — генерический раннер по ONBOARDING_QUESTIONS ───────────────────

def _onboarding_questions(data: dict) -> list[OnboardingQuestion]:
    try:
        ptype = PresentationType(data.get("presentation_type", "pitch_deck"))
    except ValueError:
        return []
    return ONBOARDING_QUESTIONS.get(ptype, [])


async def _ask_onboarding_question(target: Message, data: dict, state: FSMContext) -> None:
    questions = _onboarding_questions(data)
    idx = data.get("onboarding_index", 0)

    if idx >= len(questions):
        await _confirm_and_generate(target, data, state)
        return

    q = questions[idx]
    if q.kind == "choice":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"oq:{q.key}:{value}")]
            for label, value in (q.choices or [])
        ])
    elif q.skippable:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=q.skip_label, callback_data=f"oq_skip:{q.key}")],
        ])
    else:
        kb = None

    await target.answer(q.prompt, parse_mode="HTML", reply_markup=kb)
    await state.set_state(Gen.onboarding)


@dp.callback_query(F.data.startswith("oq:"))
async def on_onboarding_choice(call: CallbackQuery, state: FSMContext):
    _, key, value = call.data.split(":", 2)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer()

    await state.update_data(**{key: value})
    data = await state.get_data()

    # DOKLAD: "есть готовый текст/документ?" -> да — уходим за материалом
    # отдельным шагом, а не продолжаем список вопросов линейно.
    if key == "has_material" and value == "yes":
        await call.message.answer(
            "📎 Пришлите текст сообщением, или документ файлом (.pdf, .docx, .pptx, .txt)."
        )
        await state.set_state(Gen.entering_material)
        return

    data["onboarding_index"] = data.get("onboarding_index", 0) + 1
    await state.update_data(onboarding_index=data["onboarding_index"])
    await _ask_onboarding_question(call.message, data, state)


@dp.callback_query(F.data.startswith("oq_skip:"))
async def on_onboarding_skip(call: CallbackQuery, state: FSMContext):
    _, key = call.data.split(":", 1)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer()

    await state.update_data(**{key: None})
    data = await state.get_data()
    data["onboarding_index"] = data.get("onboarding_index", 0) + 1
    await state.update_data(onboarding_index=data["onboarding_index"])
    await _ask_onboarding_question(call.message, data, state)


@dp.message(Gen.onboarding)
async def on_onboarding_text(message: Message, state: FSMContext):
    data = await state.get_data()
    questions = _onboarding_questions(data)
    idx = data.get("onboarding_index", 0)

    if idx >= len(questions) or questions[idx].kind != "text":
        # Защитный случай (устаревшее состояние) — не блокируем пользователя.
        await _confirm_and_generate(message, data, state)
        return

    q = questions[idx]
    text = (message.text or "").strip()
    if len(text) > 2000:
        await message.answer("Слишком длинный текст. Сократите до 2000 символов.")
        return

    await state.update_data(**{q.key: text})
    data[q.key] = text
    data["onboarding_index"] = idx + 1
    await state.update_data(onboarding_index=data["onboarding_index"])
    await _ask_onboarding_question(message, data, state)


# ── DOKLAD: приём готового текста/документа по теме ────────────────────────────

_MATERIAL_MIME_BY_EXT = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt":  "text/plain",
}
_MATERIAL_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ


@dp.message(Gen.entering_material)
async def on_material(message: Message, state: FSMContext):
    data = await state.get_data()

    if message.document:
        doc = message.document
        ext = doc.file_name.rsplit(".", 1)[-1].lower() if doc.file_name and "." in doc.file_name else ""
        mime = _MATERIAL_MIME_BY_EXT.get(ext)
        if not mime:
            await message.answer(
                "Поддерживаются только .pdf, .docx, .pptx, .txt. "
                "Пришлите другой файл, или текст сообщением."
            )
            return
        if doc.file_size and doc.file_size > _MATERIAL_MAX_FILE_SIZE:
            await message.answer("Файл слишком большой (максимум 20 МБ). Пришлите файл поменьше или текст сообщением.")
            return

        file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(file.file_path)
        document_bytes = buf.read()

        await state.update_data(
            source_type="document",
            document_bytes=document_bytes,
            document_mime_type=mime,
            raw_text=None,
        )
    elif message.text:
        text = message.text.strip()
        if len(text) < 20:
            await message.answer("Слишком коротко — пришлите более развёрнутый текст (от 20 символов) или документ.")
            return
        await state.update_data(
            source_type="text",
            raw_text=text[:15000],
            document_bytes=None,
            document_mime_type=None,
        )
    else:
        await message.answer("Пришлите текст сообщением или документ файлом.")
        return

    data = await state.get_data()
    data["onboarding_index"] = data.get("onboarding_index", 0) + 1
    await state.update_data(onboarding_index=data["onboarding_index"])
    await _ask_onboarding_question(message, data, state)


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

    extra_line = ""
    if data.get("brief"):
        extra_line = "\n• <b>Бриф:</b> добавлен ✓"
    elif data.get("source_type") in ("text", "document"):
        extra_line = "\n• <b>Материал:</b> добавлен ✓ (доклад соберём строго по нему)"

    await message.answer(
        f"✅ <b>Создаю презентацию:</b>\n\n"
        f"• Тип: <b>{TYPE_LABELS.get(ptype, ptype)}</b>\n"
        f"• Тема: <b>{topic[:60]}{'...' if len(topic) > 60 else ''}</b>\n"
        f"• Аудитория: <b>{AUDIENCE_LABELS.get(audience, audience)}</b>\n"
        f"• Язык: <b>{lang.upper()}</b>"
        f"{extra_line}\n\n"
        f"⏳ Обычно занимает 60–90 секунд.",
        parse_mode="HTML",
    )
    await state.clear()
    await generate_and_send(message, data, watermark=watermark)


# ── Генерация ─────────────────────────────────────────────────────────────────
# Хендлер больше не генерирует презентацию сам — он кладёт job в очередь ARQ
# (worker.py, отдельный процесс) и сразу отвечает. Это единственная причина,
# по которой бот не виснет на 60-90 секунд генерации и продолжает отвечать на
# /start, /plan и прочие команды других пользователей, пока одна презентация
# ещё готовится.

async def generate_and_send(message: Message, data: dict, watermark: bool = True):
    brief = data.get("brief")
    extra = None
    if brief:
        extra = (
            f"ВАЖНО — используй эти реальные данные:\n\n{brief}\n\n"
            f"Вставляй точно: имена, цифры, контакты."
        )

    # Собираем request_data как обычный dict, а не через UserRequest(...) —
    # для DOKLAD+document source_type != "topic", а raw_text ещё не заполнен
    # (текст извлечёт content_extractor внутри воркера из document_bytes).
    # UserRequest-валидатор требует raw_text сразу, как только source_type
    # != topic, так что собирать полноценный Pydantic-объект здесь, до
    # экстракции, нельзя — тот же капкан, что уже чинили в worker.py.
    request_data = {
        "topic": data["topic"],
        "presentation_type": data["presentation_type"],
        "audience": data["audience"],
        "language": data["language"],
        "extra_instructions": extra,
        "source_type": data.get("source_type") or "topic",
        "raw_text": data.get("raw_text"),
        "content_volume": data.get("content_volume") or "medium",
    }

    job_id = uuid.uuid4().hex[:12]

    status_msg = await message.answer(
        f"⏳ <b>Генерирую, обычно занимает 60–90 секунд.</b>\n"
        f"Пришлю сюда же, как только будет готово.\n\n"
        f"<code>job {job_id}</code>",
        parse_mode="HTML",
    )

    if arq_pool is None:
        logger.error("ARQ pool is not initialized — cannot enqueue job")
        await status_msg.edit_text("❌ Очередь генерации сейчас недоступна. Попробуйте через минуту.")
        return

    await arq_pool.enqueue_job(
        "generate_presentation_job",
        job_id=job_id,
        chat_id=message.chat.id,
        user_id=message.chat.id,
        status_message_id=status_msg.message_id,
        request_data=request_data,
        document_bytes=data.get("document_bytes"),
        document_mime_type=data.get("document_mime_type"),
        urls=None,
        watermark=watermark,
        color_scheme=data.get("color_scheme", "light"),
        _job_id=job_id,
    )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    global arq_pool

    logger.info("Starting Fibonacci AI bot...")
    await init_db()

    # Playwright/LLM/S3 больше не живут в этом процессе — только очередь.
    # Реальная генерация происходит в worker.py (`arq worker.WorkerSettings`),
    # который нужно запускать отдельным процессом рядом с ботом.
    arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    logger.info("ARQ pool connected")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "pre_checkout_query"],
        )
    finally:
        await arq_pool.aclose()
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

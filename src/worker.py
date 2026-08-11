"""
ARQ worker — фоновый пайплайн генерации презентации.

Раньше вся генерация (LLM → картинки → HTML → PDF) шла синхронно внутри
aiogram-хендлера в main.py и блокировала бота на 60-90 секунд: пока один
пользователь ждёт свою презентацию, бот не может ответить на /start или
/plan никому другому. Теперь main.py только кладёт job в очередь Redis
(arq_pool.enqueue_job) и сразу отвечает — вся тяжёлая работа происходит
здесь, в отдельном процессе.

Запуск: arq worker.WorkerSettings

Пайплайн одной задачи:
  extract_content (если source_type != TOPIC)
    → generate_presentation_structure
    → fetch_images_for_slides
    → render_presentation
    → html_to_pdf
    → загрузка в S3/MinIO
    → отправка пользователю в Telegram

Задача 3 (content_extractor) намеренно вызывается ЗДЕСЬ, а не в aiogram-
хендлере — парсинг pdf/docx/pptx или скачивание URL может занять секунды,
и это ровно та работа, которую мы не хотим держать в бот-процессе.
"""

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from arq.connections import RedisSettings

from config import settings
from schemas.presentation import UserRequest, PresentationType, SlideLayout
from generation.content_extractor import extract_from_document, extract_from_url
from generation.llm import generate_presentation_structure
from generation.image_fetcher import fetch_images_for_slides
from generation.template_engine import render_presentation
from generation.pdf_renderer import html_to_pdf, get_renderer, shutdown_renderer
from generation.storage import upload_pdf
from db.session import init_db, close_db, get_session, get_or_create_user, record_presentation

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

JOB_TIMEOUT_SECONDS = 180  # LLM + картинки + PDF с запасом; см. WorkerSettings.job_timeout
MAX_CONCURRENT_JOBS = 5    # нагрузочный критерий Sprint 1: 5 параллельных не блокируют бота

# Layout'ы, которые реально умеет рисовать templates/doklad/template.html
# (см. LAYOUT_CATALOG в llm.py). SlideLayout — общий enum на все типы
# презентаций, так что модель технически может вернуть "problem"/"market"/
# "team"/"competition"/"why_now" — их шаблон не рисует. Сам шаблон на такой
# случай не остаётся пустым (см. {% else %} в template.html), но нам нужно
# видеть в логах, если модель всё же выходит за пределы каталога.
DOKLAD_SUPPORTED_LAYOUTS = frozenset({
    SlideLayout.TITLE, SlideLayout.BULLETS, SlideLayout.METRICS,
    SlideLayout.TWO_COLUMN, SlideLayout.DIAGRAM, SlideLayout.QUOTE,
    SlideLayout.TIMELINE, SlideLayout.IMAGE_FULL, SlideLayout.IMAGE_HERO,
    SlideLayout.CLOSING,
})


def _kb_after_pdf() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новая презентация", callback_data="action:new")],
    ])


async def generate_presentation_job(
    ctx: dict,
    *,
    job_id: str,
    chat_id: int,
    user_id: int,
    status_message_id: int | None,
    request_data: dict,
    document_bytes: bytes | None = None,
    document_mime_type: str | None = None,
    urls: list[str] | None = None,
    watermark: bool = True,
    color_scheme: str = "light",
) -> dict:
    bot: Bot = ctx["bot"]

    async def _status(text: str) -> None:
        if status_message_id is None:
            return
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=status_message_id)
        except Exception:
            # Сообщение могло быть удалено пользователем — это не повод валить job
            logger.debug("Could not edit status message", extra={"job_id": job_id})

    try:
        # UserRequest.raw_text is required by a model validator whenever
        # source_type != TOPIC — so we can't construct the model until
        # extraction has filled it in. Work on the raw dict first, extract,
        # then validate once into UserRequest.
        request_data = dict(request_data)
        source_type = request_data.get("source_type", "topic")

        if source_type != "topic" and not request_data.get("raw_text"):
            await _status("⚙️ Читаю материал...")
            if document_bytes is not None:
                raw_text = await extract_from_document(document_bytes, document_mime_type)
            elif urls:
                raw_text, error_urls = await extract_from_url(urls)
                if error_urls:
                    logger.warning(
                        "Some URLs failed to extract",
                        extra={"job_id": job_id, "error_urls": error_urls},
                    )
            else:
                raw_text = None
            request_data["raw_text"] = raw_text

        request = UserRequest.model_validate(request_data)

        await _status("⚙️ Пишу текст слайдов...")
        presentation = await generate_presentation_structure(request)

        if request.presentation_type == PresentationType.DOKLAD:
            unsupported = [s for s in presentation.slides if s.layout not in DOKLAD_SUPPORTED_LAYOUTS]
            if unsupported:
                logger.warning(
                    "DOKLAD: LLM returned layout(s) outside templates/doklad catalog "
                    "— rendered via the generic fallback block instead of a dedicated layout",
                    extra={
                        "job_id": job_id,
                        "slides": [{"index": s.index, "layout": s.layout.value} for s in unsupported],
                    },
                )

        # ФИО/группа докладчика — не от модели, а из профиля пользователя в
        # боте (см. main.py, "👤 Профиль"). Только для DOKLAD: это единственный
        # шаблон, который их рендерит (title/closing слайды).
        if request.presentation_type == PresentationType.DOKLAD:
            async with get_session() as session:
                if session:
                    profile_user = await get_or_create_user(session, user_id)
                    if profile_user.author_name or profile_user.author_group:
                        presentation = presentation.model_copy(update={
                            "meta": presentation.meta.model_copy(update={
                                "author_name": profile_user.author_name,
                                "author_group": profile_user.author_group,
                            })
                        })

        await _status("⚙️ Подбираю изображения...")
        image_urls = await fetch_images_for_slides(presentation.slides)

        await _status("⚙️ Собираю дизайн...")
        html = render_presentation(
            presentation,
            image_urls=image_urls,
            watermark=watermark,
            color_scheme=color_scheme,
        )

        await _status("⚙️ Рендерю PDF...")
        has_mermaid = any(s.layout.value == "diagram" for s in presentation.slides)
        pdf_bytes = await html_to_pdf(html, has_mermaid=has_mermaid)

        await upload_pdf(job_id, pdf_bytes)

        async with get_session() as session:
            if session:
                user = await get_or_create_user(session, user_id)
                await record_presentation(
                    session,
                    user=user,
                    topic=request.topic,
                    presentation_type=request.presentation_type.value,
                    audience=request.audience.value,
                    language=request.language,
                    slide_count=presentation.slide_count,
                    has_brief=bool(request.extra_instructions),
                    watermark=watermark,
                )

        if status_message_id is not None:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=status_message_id)
            except Exception:
                pass

        safe_title = "".join(
            c if c.isalnum() or c in " _-" else "_"
            for c in presentation.meta.title
        )[:40]

        caption = (
            f"✨ <b>{presentation.meta.title}</b>\n"
            f"{presentation.slide_count} слайдов"
        )
        if watermark:
            caption += "\n\n<i>Бесплатная версия · Уберите водяной знак в /plan</i>"

        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(pdf_bytes, filename=f"{safe_title}.pdf"),
            caption=caption,
            parse_mode="HTML",
            reply_markup=_kb_after_pdf(),
        )

        logger.info(
            "Job completed",
            extra={"job_id": job_id, "chat_id": chat_id, "slide_count": presentation.slide_count},
        )
        return {"status": "ok", "job_id": job_id, "slide_count": presentation.slide_count}

    except Exception as e:
        logger.exception("Job failed", extra={"job_id": job_id, "chat_id": chat_id, "error": str(e)})
        await _status("❌ Что-то пошло не так. Попробуйте ещё раз через /new")
        raise


async def startup(ctx: dict) -> None:
    ctx["bot"] = Bot(token=settings.telegram_bot_token)
    await init_db()
    await get_renderer()
    logger.info("ARQ worker started", extra={"max_jobs": MAX_CONCURRENT_JOBS})


async def shutdown(ctx: dict) -> None:
    await shutdown_renderer()
    await close_db()
    bot: Bot | None = ctx.get("bot")
    if bot is not None:
        await bot.session.close()
    logger.info("ARQ worker stopped")


class WorkerSettings:
    functions = [generate_presentation_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    job_timeout = JOB_TIMEOUT_SECONDS
    max_jobs = MAX_CONCURRENT_JOBS
    # job_id не задан пользователем -> arq сам сгенерирует; мы передаём свой
    # job_id как аргумент задачи отдельно (для статус-сообщений/S3-ключа),
    # см. main.py: enqueue_job(..., _job_id=job_id, job_id=job_id, ...)

"""
Template engine: PresentationSchema → HTML-строка.

Ответственность этого модуля:
- Загрузить нужный шаблон по presentation_type
- Прокинуть image_url в слайды (Unsplash/Pexels резолвится до рендера)
- Отрендерить Jinja2 → HTML
- Вернуть строку, готовую к передаче в Playwright
"""

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from schemas.presentation import PresentationSchema, PresentationType, Slide

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# Маппинг типа презентации → папка шаблона
# Пока все типы смотрят на pitch_deck — по мере добавления шаблонов расширяем
TEMPLATE_MAP: dict[PresentationType, str] = {
    PresentationType.PITCH_DECK:   "pitch_deck",
    PresentationType.DIPLOMA:      "pitch_deck",   # TODO: diploma template
    PresentationType.CORP_REPORT:  "pitch_deck",   # TODO: corp_report template
    PresentationType.EDUCATIONAL:  "pitch_deck",   # TODO: educational template
    PresentationType.SALES:        "pitch_deck",
    PresentationType.CONFERENCE:   "pitch_deck",
    PresentationType.ROADMAP:      "pitch_deck",
}

_env_cache: dict[str, Environment] = {}


def _get_env(template_folder: str) -> Environment:
    """Jinja2 Environment с кешем — не пересоздаём на каждый запрос."""
    if template_folder not in _env_cache:
        loader = FileSystemLoader(str(TEMPLATES_DIR / template_folder))
        _env_cache[template_folder] = Environment(
            loader=loader,
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _env_cache[template_folder]


def render_presentation(
    presentation: PresentationSchema,
    image_urls: dict[int, str] | None = None,
    team_photo_urls: dict[str, str] | None = None,
    watermark: bool = False,
    color_scheme: str = "light",
) -> str:
    """
    Рендерит HTML из схемы презентации.

    Args:
        presentation:     Валидированная схема от LLM
        image_urls:       {slide.index: url} — URL изображений от Unsplash/Pexels/Replicate
        team_photo_urls:  {member.name: url} — фото членов команды
        watermark:        Добавить водяной знак (freemium)

    Returns:
        HTML-строка, готовая к передаче в Playwright
    """
    template_folder = TEMPLATE_MAP.get(
        presentation.meta.presentation_type,
        "pitch_deck",
    )

    env = _get_env(template_folder)
    template = env.get_template("template.html")

    # Обогащаем слайды URL изображений перед рендером
    slides = _inject_image_urls(
        presentation.slides,
        image_urls or {},
        team_photo_urls or {},
    )

    html = template.render(
        meta=presentation.meta,
        slides=slides,
        watermark=watermark,
        color_scheme=color_scheme,
    )

    logger.info(
        "Template rendered",
        extra={
            "type": presentation.meta.presentation_type,
            "slides": len(slides),
            "watermark": watermark,
            "template": template_folder,
        },
    )

    return html


def _inject_image_urls(
    slides: list[Slide],
    image_urls: dict[int, str],
    team_photo_urls: dict[str, str],
) -> list[Slide]:
    """
    Прокидывает URL в слайды перед рендером.

    Мутировать Pydantic-объекты напрямую — плохая практика,
    поэтому делаем shallow copy через model_copy.
    """
    result = []
    for slide in slides:
        updated = slide.model_copy()

        # URL изображения для слайда
        if slide.index in image_urls:
            updated = updated.model_copy(update={"image_url": image_urls[slide.index]})
        else:
            updated = updated.model_copy(update={"image_url": None})

        # Фото членов команды
        if slide.team_members and team_photo_urls:
            enriched_members = []
            for member in slide.team_members:
                if member.name in team_photo_urls:
                    enriched = member.model_copy(
                        update={"photo_url": team_photo_urls[member.name]}
                    )
                else:
                    enriched = member.model_copy(update={"photo_url": None})
                enriched_members.append(enriched)
            updated = updated.model_copy(update={"team_members": enriched_members})

        result.append(updated)

    return result

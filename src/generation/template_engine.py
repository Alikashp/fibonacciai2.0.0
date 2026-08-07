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

from schemas.presentation import PresentationSchema, PresentationType, ContentSourceType, Slide, SlideLayout
from layouts.zones import get_zones

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# Маппинг типа презентации → папка шаблона
# Пока не все типы имеют собственный шаблон — недостающие временно смотрят на pitch_deck
TEMPLATE_MAP: dict[PresentationType, str] = {
    PresentationType.PITCH_DECK:   "pitch_deck",
    PresentationType.DIPLOMA:      "pitch_deck",   # TODO: diploma template
    PresentationType.CORP_REPORT:  "corp_report",
    PresentationType.EDUCATIONAL:  "pitch_deck",   # TODO: educational template
    PresentationType.SALES:        "pitch_deck",
    PresentationType.CONFERENCE:   "conference",
    PresentationType.ROADMAP:      "pitch_deck",
    # DOKLAD не смотрит сюда напрямую — см. _pick_template_folder(): для него
    # шаблон выбирается по source_type запроса, а не по presentation_type.
    PresentationType.DOKLAD:       "conference",
}

# DOKLAD — единственный тип, где шаблон определяется не presentation_type,
# а source_type исходного запроса: "с нуля по теме" выглядит как доклад
# (conference), "по готовому материалу" — как отчёт по проделанной работе
# (corp_report). См. Sprint: "Задача 1. Новый тип DOKLAD с веткой по source_type".
DOKLAD_TEMPLATE_BY_SOURCE: dict[ContentSourceType, str] = {
    ContentSourceType.TOPIC:    "conference",
    ContentSourceType.TEXT:     "corp_report",
    ContentSourceType.DOCUMENT: "corp_report",
    ContentSourceType.URL:      "corp_report",
}


def _pick_template_folder(
    presentation_type: PresentationType,
    source_type: ContentSourceType | None,
) -> str:
    if presentation_type == PresentationType.DOKLAD:
        return DOKLAD_TEMPLATE_BY_SOURCE.get(
            source_type or ContentSourceType.TOPIC,
            "conference",
        )
    return TEMPLATE_MAP.get(presentation_type, "pitch_deck")

_env_cache: dict[str, Environment] = {}


def zone_style(layout: SlideLayout, key: str) -> str:
    """
    Jinja-хелпер: inline CSS-позиционирование элемента слайда из реестра зон
    (layouts/zones.py) — единый источник координат для HTML/CSS и (в будущем)
    python-pptx рендерера. Возвращает пустую строку, если для layout/key зона
    не зарегистрирована — элемент тогда просто не позиционируется абсолютно.
    """
    zone = get_zones(layout).zones.get(key)
    return zone.as_style() if zone else ""


def _get_env(template_folder: str) -> Environment:
    """Jinja2 Environment с кешем — не пересоздаём на каждый запрос."""
    if template_folder not in _env_cache:
        loader = FileSystemLoader(str(TEMPLATES_DIR / template_folder))
        env = Environment(
            loader=loader,
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.globals["zone_style"] = zone_style
        _env_cache[template_folder] = env
    return _env_cache[template_folder]


def render_presentation(
    presentation: PresentationSchema,
    image_urls: dict[int, str] | None = None,
    team_photo_urls: dict[str, str] | None = None,
    watermark: bool = False,
    color_scheme: str = "light",
    source_type: ContentSourceType | None = None,
) -> str:
    """
    Рендерит HTML из схемы презентации.

    Args:
        presentation:     Валидированная схема от LLM
        image_urls:       {slide.index: url} — URL изображений от Unsplash/Pexels/Replicate
        team_photo_urls:  {member.name: url} — фото членов команды
        watermark:        Добавить водяной знак (freemium)
        source_type:      Источник исходного запроса (UserRequest.source_type).
                           Используется только для PresentationType.DOKLAD, где
                           именно source_type, а не presentation_type, решает,
                           какой шаблон (conference/corp_report) выбрать.

    Returns:
        HTML-строка, готовая к передаче в Playwright
    """
    template_folder = _pick_template_folder(presentation.meta.presentation_type, source_type)

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

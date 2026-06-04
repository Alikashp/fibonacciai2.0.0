"""
Контракт данных: LLM → Pydantic → Jinja2-шаблон

Правила:
- Все поля Optional с дефолтами — LLM иногда пропускает поля, падать нельзя
- Валидация длин — защита от LLM-галлюцинаций (500-слов заголовок убьёт шаблон)
- Enum для типов слайдов — шаблонизатор знает какой layout рендерить
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ─── Enums ────────────────────────────────────────────────────────────────────

class PresentationType(str, Enum):
    PITCH_DECK   = "pitch_deck"
    DIPLOMA      = "diploma"
    CORP_REPORT  = "corp_report"
    EDUCATIONAL  = "educational"
    SALES        = "sales"
    CONFERENCE   = "conference"
    ROADMAP      = "roadmap"


class AudienceType(str, Enum):
    INVESTORS    = "investors"
    CLIENTS      = "clients"
    STUDENTS     = "students"
    COLLEAGUES   = "colleagues"
    MANAGEMENT   = "management"
    GENERAL      = "general"


class SlideLayout(str, Enum):
    """
    Какой HTML-layout рендерить для этого слайда.
    Шаблонизатор делает switch по этому полю.
    """
    TITLE        = "title"         # Титульный: большой заголовок, подзаголовок, имя
    PROBLEM      = "problem"       # Проблема: текст + иконка или иллюстрация
    SOLUTION     = "solution"      # Решение: текст + изображение справа
    METRICS      = "metrics"       # Метрики: 3-4 больших числа с подписями
    BULLETS      = "bullets"       # Список: заголовок + 3-6 пунктов
    TWO_COLUMN   = "two_column"    # Два столбца: текст + текст или текст + список
    IMAGE_FULL   = "image_full"    # Полноэкранное изображение с подписью
    DIAGRAM      = "diagram"       # Mermaid-диаграмма + пояснение
    QUOTE        = "quote"         # Цитата: большой текст + автор
    TEAM         = "team"          # Команда: карточки с фото и должностями
    TIMELINE     = "timeline"      # Таймлайн: горизонтальный или вертикальный
    CLOSING      = "closing"       # Финальный: призыв к действию + контакты


# ─── Атомарные компоненты слайда ──────────────────────────────────────────────

class MetricItem(BaseModel):
    value: str = Field(..., max_length=20)    # "2.5M", "$10K", "94%"
    label: str = Field(..., max_length=60)    # "Активных пользователей"
    trend: Optional[str] = None               # "+12% за квартал" — опционально

    @field_validator("value")
    @classmethod
    def value_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Метрика не может быть пустой")
        return v


class BulletPoint(BaseModel):
    text: str = Field(..., max_length=200)
    emphasis: bool = False                    # Выделить жирным в шаблоне


class TeamMember(BaseModel):
    name: str = Field(..., max_length=80)
    role: str = Field(..., max_length=100)
    bio: Optional[str] = Field(None, max_length=200)
    photo_query: Optional[str] = None        # Поисковый запрос для Unsplash


class TimelineItem(BaseModel):
    date: str = Field(..., max_length=30)    # "Q1 2025", "Март 2024"
    title: str = Field(..., max_length=100)
    description: Optional[str] = Field(None, max_length=200)


class TwoColumnContent(BaseModel):
    left_title: Optional[str] = Field(None, max_length=100)
    left_text: Optional[str] = Field(None, max_length=500)
    left_bullets: list[BulletPoint] = Field(default_factory=list, max_length=5)
    right_title: Optional[str] = Field(None, max_length=100)
    right_text: Optional[str] = Field(None, max_length=500)
    right_bullets: list[BulletPoint] = Field(default_factory=list, max_length=5)


# ─── Слайд ────────────────────────────────────────────────────────────────────

class Slide(BaseModel):
    index: int                                          # Порядковый номер, 1-based
    layout: SlideLayout
    title: Optional[str] = Field(None, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    body_text: Optional[str] = Field(None, max_length=600)

    # Специфичные для layout поля — только одно будет заполнено
    bullets: list[BulletPoint] = Field(default_factory=list, max_length=6)
    metrics: list[MetricItem] = Field(default_factory=list, max_length=4)
    team_members: list[TeamMember] = Field(default_factory=list, max_length=6)
    timeline_items: list[TimelineItem] = Field(default_factory=list, max_length=8)
    two_column: Optional[TwoColumnContent] = None

    # Медиа
    image_query: Optional[str] = Field(None, max_length=100)
    # Поисковый запрос для Unsplash/Pexels — НЕ URL.
    # Пример: "startup team working office" (на английском — лучше результаты)

    mermaid_code: Optional[str] = None
    # Только для layout=DIAGRAM. Raw Mermaid-синтаксис.

    speaker_notes: Optional[str] = Field(None, max_length=500)
    # Заметки для докладчика — не рендерятся в PDF, хранятся отдельно

    @field_validator("bullets")
    @classmethod
    def validate_bullets_for_layout(cls, v: list, info) -> list:
        # Предупреждение: bullets без layout=BULLETS — probably ошибка LLM
        # Не падаем, просто возвращаем как есть — шаблон сам разберётся
        return v

    @field_validator("mermaid_code")
    @classmethod
    def validate_mermaid(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Базовая проверка — Mermaid должен начинаться с известного типа
        allowed_starts = (
            "graph ", "graph\n",
            "flowchart ", "flowchart\n",
            "sequenceDiagram", "classDiagram",
            "pie ", "pie\n",
            "gantt", "mindmap",
            "quadrantChart", "xychart-beta",
        )
        stripped = v.strip()
        if not any(stripped.startswith(s) for s in allowed_starts):
            # Не падаем — просто убираем, шаблон отрендерит без диаграммы
            return None
        return v


# ─── Мета-данные презентации ──────────────────────────────────────────────────

class PresentationMeta(BaseModel):
    title: str = Field(..., max_length=150)
    subtitle: Optional[str] = Field(None, max_length=200)
    author: Optional[str] = Field(None, max_length=100)
    company: Optional[str] = Field(None, max_length=100)
    date: Optional[str] = Field(None, max_length=50)   # "Июнь 2025"
    language: str = "ru"
    presentation_type: PresentationType
    audience: AudienceType
    color_scheme: str = "default"
    # В будущем: "corporate_blue", "startup_dark", "academic" и т.д.
    # Для MVP — один дефолтный, но поле уже в схеме


# ─── Корневая схема — то что возвращает LLM ───────────────────────────────────

class PresentationSchema(BaseModel):
    meta: PresentationMeta
    slides: list[Slide] = Field(..., min_length=5, max_length=20)

    @field_validator("slides")
    @classmethod
    def validate_slide_order(cls, slides: list[Slide]) -> list[Slide]:
        # Первый слайд должен быть TITLE
        if slides and slides[0].layout != SlideLayout.TITLE:
            # Мягкая коррекция: меняем layout, не падаем
            slides[0].layout = SlideLayout.TITLE
        # Последний слайд должен быть CLOSING
        if slides and slides[-1].layout != SlideLayout.CLOSING:
            slides[-1].layout = SlideLayout.CLOSING
        # Переиндексируем на случай если LLM напутал с index
        for i, slide in enumerate(slides, start=1):
            slide.index = i
        return slides

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    def get_slides_by_layout(self, layout: SlideLayout) -> list[Slide]:
        return [s for s in self.slides if s.layout == layout]


# ─── Входные параметры от пользователя ───────────────────────────────────────

class UserRequest(BaseModel):
    """То что приходит от Telegram бота."""
    topic: str = Field(..., min_length=3, max_length=300)
    presentation_type: PresentationType
    audience: AudienceType
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    extra_instructions: Optional[str] = Field(None, max_length=500)
    # Дополнительные пожелания пользователя: "добавь слайд про конкурентов"
    # Для MVP — опционально, но поле уже есть

    slide_count_hint: Optional[int] = Field(None, ge=5, le=20)
    # Пользователь может попросить "сделай 10 слайдов"
    # Если None — LLM сам решает сколько нужно (обычно 8-12)

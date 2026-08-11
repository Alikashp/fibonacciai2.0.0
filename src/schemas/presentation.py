from enum import Enum
from typing import Callable, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class PresentationType(str, Enum):
    PITCH_DECK   = "pitch_deck"
    DIPLOMA      = "diploma"
    CORP_REPORT  = "corp_report"
    EDUCATIONAL  = "educational"
    SALES        = "sales"
    CONFERENCE   = "conference"
    ROADMAP      = "roadmap"
    DOKLAD       = "doklad"


class AudienceType(str, Enum):
    INVESTORS    = "investors"
    CLIENTS      = "clients"
    STUDENTS     = "students"
    COLLEAGUES   = "colleagues"
    MANAGEMENT   = "management"
    GENERAL      = "general"
    SCHOOLKIDS   = "schoolkids"
    FRIENDS      = "friends"


class ContentVolume(str, Enum):
    SHORT  = "short"
    MEDIUM = "medium"
    LONG   = "long"


class ContentSourceType(str, Enum):
    TOPIC    = "topic"
    TEXT     = "text"
    DOCUMENT = "document"
    URL      = "url"


class SlideLayout(str, Enum):
    TITLE        = "title"
    PROBLEM      = "problem"
    SOLUTION     = "solution"
    WHY_NOW      = "why_now"
    MARKET       = "market"
    METRICS      = "metrics"
    BULLETS      = "bullets"
    TWO_COLUMN   = "two_column"
    COMPETITION  = "competition"
    TEAM         = "team"
    TIMELINE     = "timeline"
    DIAGRAM      = "diagram"
    QUOTE        = "quote"
    IMAGE_FULL   = "image_full"
    IMAGE_HERO   = "image_hero"
    CLOSING      = "closing"


class MetricItem(BaseModel):
    value: str = Field(..., max_length=20)
    label: str = Field(..., max_length=80)
    trend: Optional[str] = Field(None, max_length=80)
    source: Optional[str] = Field(None, max_length=400)


# Закрытый список допустимых иконок (Tabler Icons) для BulletPoint.icon —
# только эти имена (без префикса "ti-") реально подключены self-hosted
# webfont'ом в templates/doklad/template.html. Всё остальное, включая
# отсутствие поля, схлопывается в нейтральную точку — см. validate_icon.
ALLOWED_BULLET_ICONS = frozenset({
    "file-text", "list", "check", "alert-triangle", "clock", "calendar",
    "users", "user", "coin", "chart-bar", "target", "bulb", "search",
    "settings", "lock", "message-circle", "folder", "arrow-up",
    "arrow-right", "help-circle", "star", "bolt", "shield", "refresh",
    "link",
})
FALLBACK_BULLET_ICON = "point"


class BulletPoint(BaseModel):
    text: str = Field(..., max_length=400)
    subtitle: Optional[str] = Field(None, max_length=80)
    icon: Optional[str] = Field(None, max_length=40)
    emphasis: bool = False

    @field_validator("icon")
    @classmethod
    def validate_icon(cls, v):
        if v is None:
            return v
        if v not in ALLOWED_BULLET_ICONS:
            return FALLBACK_BULLET_ICON
        return v


class TeamMember(BaseModel):
    name: str = Field(..., max_length=80)
    role: str = Field(..., max_length=100)
    bio: Optional[str] = Field(None, max_length=200)
    gender: str = Field(default="male")
    photo_query: Optional[str] = None
    photo_url: Optional[str] = None


class TimelineItem(BaseModel):
    date: str = Field(..., max_length=30)
    title: str = Field(..., max_length=100)
    description: Optional[str] = Field(None, max_length=300)


class TwoColumnContent(BaseModel):
    left_title: Optional[str] = Field(None, max_length=100)
    left_text: Optional[str] = Field(None, max_length=500)
    left_bullets: list[BulletPoint] = Field(default_factory=list, max_length=6)
    right_title: Optional[str] = Field(None, max_length=100)
    right_text: Optional[str] = Field(None, max_length=500)
    right_bullets: list[BulletPoint] = Field(default_factory=list, max_length=6)
    right_preferred: bool = False

    @field_validator("left_bullets", "right_bullets", mode="before")
    @classmethod
    def coerce_bullets(cls, v):
        if not isinstance(v, list):
            return []
        return [
            {"text": item, "emphasis": False} if isinstance(item, str) else item
            for item in v
        ]


class CompetitorItem(BaseModel):
    name: str = Field(..., max_length=60)


class CompetitionFeature(BaseModel):
    name: str = Field(..., max_length=80)
    values: dict[str, str] = Field(default_factory=dict)
    # values: {competitor_name: "yes"|"no"|"partial text"}


class CompetitionTable(BaseModel):
    our_name: str = Field(..., max_length=60)
    competitors: list[CompetitorItem] = Field(..., max_length=5)
    features: list[CompetitionFeature] = Field(..., max_length=7)


class Slide(BaseModel):
    index: int
    layout: SlideLayout
    title: Optional[str] = Field(None, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    body_text: Optional[str] = Field(None, max_length=600)
    bullets: list[BulletPoint] = Field(default_factory=list, max_length=6)
    metrics: list[MetricItem] = Field(default_factory=list, max_length=4)
    team_members: list[TeamMember] = Field(default_factory=list, max_length=6)
    timeline_items: list[TimelineItem] = Field(default_factory=list, max_length=8)
    two_column: Optional[TwoColumnContent] = None
    competition_table: Optional[CompetitionTable] = None
    image_query: Optional[str] = Field(None, max_length=100)
    image_url: Optional[str] = None
    mermaid_code: Optional[str] = None
    speaker_notes: Optional[str] = Field(None, max_length=500)

    @field_validator("mermaid_code")
    @classmethod
    def validate_mermaid(cls, v):
        if v is None:
            return v
        allowed = ("graph ","graph\n","flowchart ","flowchart\n","sequenceDiagram","classDiagram","pie ","pie\n","gantt","mindmap","xychart-beta")
        if not any(v.strip().startswith(s) for s in allowed):
            return None
        return v


class PresentationMeta(BaseModel):
    title: str = Field(..., max_length=150)
    subtitle: Optional[str] = Field(None, max_length=200)
    author: Optional[str] = Field(None, max_length=100)
    company: Optional[str] = Field(None, max_length=100)
    date: Optional[str] = Field(None, max_length=50)
    language: str = "ru"
    presentation_type: PresentationType
    audience: AudienceType
    color_scheme: str = "default"
    # ФИО докладчика / группа-организация — НЕ заполняются моделью, а
    # подставляются в worker.py из настроек пользователя в боте (профиль).
    author_name: Optional[str] = None
    author_group: Optional[str] = None


def _has_two_column_content(slide: "Slide") -> bool:
    tc = slide.two_column
    return bool(tc) and bool(
        tc.left_bullets or tc.right_bullets
        or (tc.left_text and tc.left_text.strip())
        or (tc.right_text and tc.right_text.strip())
    )


# Требование к КОНКРЕТНОМУ полю, которое реально рисует каждый layout —
# просто "хоть что-то заполнено" пропускало слайды вида "title+subtitle
# есть, а bullets/metrics/two_column/mermaid_code пустые" (см. прод: PDF,
# где title="Логика управления требованиями" и subtitle="Понимание
# процесса" — и больше НИЧЕГО, тело слайда пустое). Явно перечислены
# только layout'ы с однозначным "главным" полем по LAYOUT_CATALOG
# (llm.py); для остальных (problem/solution/market/team/competition —
# используются в pitch_deck со своей жёсткой структурой, где точный набор
# полей не так однозначен) — общая проверка "хоть что-то из содержательных
# полей заполнено", как было раньше.
_LAYOUT_REQUIRES: dict[SlideLayout, Callable[["Slide"], bool]] = {
    SlideLayout.BULLETS:    lambda s: bool(s.bullets),
    SlideLayout.WHY_NOW:    lambda s: bool(s.bullets),
    SlideLayout.METRICS:    lambda s: bool(s.metrics),
    SlideLayout.TWO_COLUMN: _has_two_column_content,
    SlideLayout.DIAGRAM:    lambda s: bool(s.mermaid_code and s.mermaid_code.strip()),
    SlideLayout.QUOTE:      lambda s: bool(s.body_text and s.body_text.strip()),
    SlideLayout.TIMELINE:   lambda s: bool(s.timeline_items),
    SlideLayout.IMAGE_FULL: lambda s: bool(s.image_query and s.image_query.strip()),
    SlideLayout.IMAGE_HERO: lambda s: bool(s.image_query and s.image_query.strip()),
    SlideLayout.TEAM:       lambda s: bool(s.team_members),
    SlideLayout.COMPETITION: lambda s: bool(s.competition_table),
}


def _slide_has_required_content(slide: "Slide") -> bool:
    check = _LAYOUT_REQUIRES.get(slide.layout)
    if check is not None:
        return check(slide)
    return bool(
        (slide.title and slide.title.strip()) or (slide.subtitle and slide.subtitle.strip())
        or (slide.body_text and slide.body_text.strip()) or slide.bullets or slide.metrics
        or slide.team_members or slide.timeline_items or slide.two_column or slide.competition_table
        or (slide.image_query and slide.image_query.strip()) or (slide.mermaid_code and slide.mermaid_code.strip())
    )


class PresentationSchema(BaseModel):
    meta: PresentationMeta
    slides: list[Slide] = Field(..., min_length=5, max_length=20)

    @field_validator("slides")
    @classmethod
    def validate_slides(cls, slides):
        if slides and slides[0].layout != SlideLayout.TITLE:
            slides[0].layout = SlideLayout.TITLE
        if slides and slides[-1].layout != SlideLayout.CLOSING:
            slides[-1].layout = SlideLayout.CLOSING
        for i, slide in enumerate(slides, start=1):
            slide.index = i

        # Наблюдали в проде (реальный сгенерированный PDF): модель иногда
        # ставит layout на слайд, но не заполняет поле, которое этот layout
        # реально рисует — title+subtitle есть, тело пустое. title/closing
        # не проверяем: их ветки в шаблоне статичны и не зависят от полей
        # слайда. Поднимаем ошибку валидации вместо тихой отправки пустого
        # слайда пользователю — generate_presentation_structure() уже
        # оборачивает вызов LLM в @retry (tenacity, до 3 попыток), так что
        # это ValueError уводит в повторный вызов модели, а не в готовый
        # PDF с дырой.
        for slide in slides:
            if slide.layout in (SlideLayout.TITLE, SlideLayout.CLOSING):
                continue
            if not _slide_has_required_content(slide):
                raise ValueError(
                    f"Slide {slide.index} (layout={slide.layout.value}) is missing the "
                    f"content that layout actually renders (title/subtitle alone isn't enough)"
                )

        return slides

    @property
    def slide_count(self):
        return len(self.slides)


class UserRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=300)
    presentation_type: PresentationType
    audience: AudienceType
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    extra_instructions: Optional[str] = Field(None, max_length=2000)
    slide_count_hint: Optional[int] = Field(None, ge=5, le=20)
    source_type: ContentSourceType = ContentSourceType.TOPIC
    raw_text: Optional[str] = Field(None, max_length=15000)
    content_volume: ContentVolume = ContentVolume.MEDIUM

    @model_validator(mode="after")
    def validate_raw_text(self):
        if self.source_type != ContentSourceType.TOPIC and not (self.raw_text and self.raw_text.strip()):
            raise ValueError("raw_text обязателен, если source_type != topic")
        return self

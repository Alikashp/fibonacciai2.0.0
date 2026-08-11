from enum import Enum
from typing import Optional
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

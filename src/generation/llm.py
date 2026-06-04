import json
import logging
from string import Template

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from schemas.presentation import (
    PresentationSchema,
    UserRequest,
    PresentationType,
    AudienceType,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
)

SYSTEM_PROMPT = """\
Ты — эксперт по созданию профессиональных презентаций и бизнес-аналитик.
Твоя задача: сгенерировать структуру презентации в формате JSON.

ЯЗЫК — КРИТИЧЕСКИ ВАЖНО:
- Весь текст презентации (title, subtitle, body_text, bullets, labels, trends) пиши ТОЛЬКО на языке: {language}
- Если язык казахский (kk) — пиши на казахском языке, НЕ на русском
- Если язык узбекский (uz) — пиши на узбекском языке, НЕ на русском
- Это правило не имеет исключений. Каждое поле каждого слайда — на указанном языке

ПРАВИЛА JSON:
1. Возвращай ТОЛЬКО валидный JSON. Никаких пояснений, markdown, ```json блоков.
2. Первый слайд ВСЕГДА layout="title", последний ВСЕГДА layout="closing".
3. image_query — ВСЕГДА на английском языке (для поиска фото на Unsplash).
4. mermaid_code — только для layout="diagram", валидный Mermaid-синтаксис.
5. bullets.text — конкретно и ёмко, максимум 15 слов на пункт.

ДАННЫЕ И ЦИФРЫ — ВАЖНО:
- Используй реальные данные из своих знаний: размеры рынков, статистику, исследования
- Если точных данных нет — используй данные смежной отрасли с пометкой "оценочно" или "по данным смежных рынков"
- Указывай источник в trend поле: "по данным Grand View Research", "оценка Statista" и т.д.
- НЕ пиши [ЦИФРА] — это бесполезно для пользователя. Лучше реалистичная оценка чем заглушка.
- Если данных совсем нет — пиши диапазон: "$5-15B" вместо "[ЦИФРА]"

ТРЕБОВАНИЯ К СЛАЙДУ layout="problem":
- ОБЯЗАТЕЛЬНО заполни поле metrics: 2-3 конкретные цифры подтверждающие проблему
- ОБЯЗАТЕЛЬНО заполни поле bullets: 2-3 конкретных факта или последствия проблемы
- body_text — одно сильное утверждение о масштабе проблемы
- Не оставляй слайд только с заголовком и одной строкой текста

ТРЕБОВАНИЯ К СЛАЙДУ layout="metrics":
- Минимум 3 метрики, максимум 4
- Каждая метрика должна иметь реальное или оценочное значение
- trend обязателен для каждой метрики

ТИПЫ СЛАЙДОВ:
- title: только первый слайд
- problem: проблема рынка — ОБЯЗАТЕЛЬНО с цифрами и bullets
- solution: как продукт решает проблему
- metrics: ключевые показатели рынка или тракшн
- bullets: список тезисов, преимуществ, шагов
- two_column: сравнение, до/после, плюсы/минусы
- image_full: эмоциональный визуальный слайд
- diagram: процессы и схемы (Mermaid)
- quote: цитата клиента или эксперта
- team: команда проекта
- timeline: план, roadmap
- closing: призыв к действию и контакты

СХЕМА JSON:
{schema}
"""

USER_PROMPT_TEMPLATE = Template("""\
Создай презентацию:

Тема: $topic
Тип: $presentation_type
Аудитория: $audience
Язык: $language — ВСЕ тексты слайдов только на этом языке
$slide_count_instruction
$extra_instructions_block

Контекст по типу:
$type_context

Контекст по аудитории:
$audience_context

Сгенерируй $slide_count_hint слайдов. Верни только JSON.
""")

TYPE_CONTEXTS: dict[PresentationType, str] = {
    PresentationType.PITCH_DECK: (
        "Питч для инвесторов. Структура: проблема (с цифрами) → решение → "
        "объём рынка (реальные данные) → бизнес-модель → тракшн → команда → "
        "финансовый план → инвестиционное предложение."
    ),
    PresentationType.DIPLOMA: (
        "Защита дипломной работы. Структура: тема и актуальность → цель и задачи → "
        "методология → результаты → выводы. Академический стиль."
    ),
    PresentationType.CORP_REPORT: (
        "Корпоративный отчёт для руководства. Структура: резюме периода → "
        "ключевые метрики → выполнение плана → проблемы → планы. Данные и факты."
    ),
    PresentationType.EDUCATIONAL: (
        "Обучающая презентация. Структура: введение → ключевые концепции → "
        "примеры → практика → резюме. Простой язык, логическая прогрессия."
    ),
    PresentationType.SALES: (
        "Коммерческое предложение. Структура: боль клиента → решение → "
        "преимущества → кейсы → условия и цены → следующий шаг."
    ),
    PresentationType.CONFERENCE: (
        "Доклад на конференции. Структура: тезис → контекст → "
        "ключевые идеи → доказательства → выводы."
    ),
    PresentationType.ROADMAP: (
        "Стратегия и роадмап. Структура: текущее состояние → цели → "
        "приоритеты → план по кварталам → ресурсы → ожидаемые результаты."
    ),
}

AUDIENCE_CONTEXTS: dict[AudienceType, str] = {
    AudienceType.INVESTORS: (
        "Инвесторы хотят видеть размер рынка, бизнес-модель, тракшн, команду. "
        "Язык ROI и масштабируемости. Цифры важнее слов."
    ),
    AudienceType.CLIENTS: (
        "Клиенты хотят понять как это решает их проблему и каков результат. "
        "Конкретные кейсы важнее теории."
    ),
    AudienceType.STUDENTS: (
        "Академическая аудитория. Строгость, методология, логическая последовательность."
    ),
    AudienceType.COLLEAGUES: (
        "Коллеги знают контекст. Фокус на сути, решениях, следующих шагах."
    ),
    AudienceType.MANAGEMENT: (
        "Руководство ценит краткость и цифры. Выводы сначала, потом детали."
    ),
    AudienceType.GENERAL: (
        "Широкая аудитория. Простой язык, понятные аналогии."
    ),
}


def _build_user_prompt(request: UserRequest) -> str:
    slide_count = request.slide_count_hint or _default_slide_count(request.presentation_type)
    slide_count_instruction = f"Количество слайдов: {slide_count} (строго)."
    extra_block = ""
    if request.extra_instructions:
        extra_block = f"Дополнительные пожелания: {request.extra_instructions}"
    return USER_PROMPT_TEMPLATE.substitute(
        topic=request.topic,
        presentation_type=request.presentation_type.value,
        audience=request.audience.value,
        language=request.language,
        slide_count_hint=slide_count,
        slide_count_instruction=slide_count_instruction,
        extra_instructions_block=extra_block,
        type_context=TYPE_CONTEXTS.get(request.presentation_type, ""),
        audience_context=AUDIENCE_CONTEXTS.get(request.audience, ""),
    )


def _default_slide_count(presentation_type: PresentationType) -> int:
    defaults = {
        PresentationType.PITCH_DECK:  12,
        PresentationType.DIPLOMA:     14,
        PresentationType.CORP_REPORT: 10,
        PresentationType.EDUCATIONAL: 12,
        PresentationType.SALES:       10,
        PresentationType.CONFERENCE:  10,
        PresentationType.ROADMAP:     12,
    }
    return defaults.get(presentation_type, 10)


def _get_json_schema() -> str:
    return """{
  "meta": {
    "title": "string",
    "subtitle": "string | null",
    "author": "string | null",
    "company": "string | null",
    "date": "string | null",
    "language": "string (ru/en/uz/kk/es/ar/zh/de)",
    "presentation_type": "pitch_deck|diploma|corp_report|educational|sales|conference|roadmap",
    "audience": "investors|clients|students|colleagues|management|general",
    "color_scheme": "default"
  },
  "slides": [
    {
      "index": 1,
      "layout": "title|problem|solution|metrics|bullets|two_column|image_full|diagram|quote|team|timeline|closing",
      "title": "string | null",
      "subtitle": "string | null",
      "body_text": "string | null",
      "bullets": [{"text": "string", "emphasis": false}],
      "metrics": [{"value": "string", "label": "string", "trend": "string | null"}],
      "team_members": [{"name": "string", "role": "string", "bio": "string | null", "photo_query": "string | null"}],
      "timeline_items": [{"date": "string", "title": "string", "description": "string | null"}],
      "two_column": {"left_title": "string | null", "left_text": "string | null", "left_bullets": [], "right_title": "string | null", "right_text": "string | null", "right_bullets": []},
      "image_query": "string | null (ВСЕГДА на английском)",
      "mermaid_code": "string | null",
      "speaker_notes": "string | null"
    }
  ]
}"""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def generate_presentation_structure(
    request: UserRequest,
) -> PresentationSchema:
    system_prompt = SYSTEM_PROMPT.format(
        schema=_get_json_schema(),
        language=request.language,
    )
    user_prompt = _build_user_prompt(request)

    logger.info("Generating presentation", extra={
        "topic": request.topic[:50],
        "type": request.presentation_type,
        "language": request.language,
        "model": settings.openai_model,
    })

    response = await client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.7,
        max_tokens=4000,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw_json = response.choices[0].message.content

    try:
        data = json.loads(raw_json)
        presentation = PresentationSchema.model_validate(data)
    except json.JSONDecodeError as e:
        logger.error("LLM returned invalid JSON", extra={"error": str(e)})
        raise
    except Exception as e:
        logger.error("Schema validation failed", extra={"error": str(e)})
        raise

    logger.info("Presentation generated", extra={
        "slide_count": presentation.slide_count,
        "title": presentation.meta.title[:50],
    })

    return presentation

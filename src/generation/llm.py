import json
import logging
from string import Template

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from schemas.presentation import (
    PresentationSchema, UserRequest, PresentationType, AudienceType,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
)

SYSTEM_PROMPT = """\
Ты — эксперт по созданию профессиональных презентаций и бизнес-аналитик.
Генерируй структуру презентации строго в формате JSON.

ЯЗЫК — КРИТИЧЕСКИ ВАЖНО:
- Весь текст (title, subtitle, body_text, bullets, labels, trends) пиши ТОЛЬКО на языке: {language}
- Казахский (kk) — пиши на казахском, НЕ на русском
- Узбекский (uz) — пиши на узбекском, НЕ на русском
- image_query — ВСЕГДА на английском (для поиска фото)

ПРАВИЛА JSON:
1. Возвращай ТОЛЬКО валидный JSON без пояснений и markdown.
2. Первый слайд ВСЕГДА layout="title", последний ВСЕГДА layout="closing".
3. mermaid_code — только для layout="diagram", валидный Mermaid-синтаксис.

ДАННЫЕ И ЦИФРЫ:
- Используй реальные данные из своих знаний: рынки, статистику, исследования.
- Источник указывай в поле trend или source: "Grand View Research, 2024".
- Если точных данных нет — используй данные смежной отрасли с пометкой "оценочно".
- НЕ пиши [ЦИФРА] — лучше реалистичный диапазон "$5-15B".

СТРУКТУРА ПИТЧ-ДЕКА (строго 11 слайдов):
1. title — название, подзаголовок. ОБЯЗАТЕЛЬНО поставь image_query — фото для фона (на английском, например: "modern medical clinic reception" или "healthy lifestyle nature")
2. problem — layout="problem", ОБЯЗАТЕЛЬНО metrics с 2-3 цифрами + источниками
3. solution — layout="solution", bullets с преимуществами + metrics с результатами
4. why_now — layout="bullets", 4 тренда почему сейчас идеальное время
5. market — layout="market", 3 метрики TAM/SAM/SOM с источниками и расчётами
6. biz_model — layout="two_column", потоки дохода + юнит-экономика (CAC, LTV, LTV/CAC)
7. traction — layout="metrics", 3 ключевые метрики роста
8. competition — layout="competition", таблица сравнения с конкурентами
9. team — layout="team", 3-4 члена команды с gender (male/female) по имени
10. roadmap — layout="timeline", 4 этапа + body_text с распределением инвестиций
11. closing — layout="closing", запрос и контакты

COMPETITION TABLE — СТРОГИЕ ПРАВИЛА:
1. competitors: ТОЛЬКО реальные названия компаний на этом рынке. ЗАПРЕЩЕНО писать "Competitor A", "Конкурент 1" и любые заглушки.
2. features: МИНИМУМ 5 критериев, релевантных для данной отрасли. Меньше 5 — ошибка.
3. Ключи в values ДОЛЖНЫ точно совпадать с именами в competitors.
4. Значения: "yes", "no", или текст до 10 символов ("частично", "платно").

Пример для медицины:
"competition_table": {{
  "our_name": "ЗдоровыйЯ",
  "competitors": [{{"name": "DocDoc"}}, {{"name": "НаПоправку"}}, {{"name": "Zoon"}}],
  "features": [
    {{"name": "ИИ-подбор врача", "values": {{"DocDoc": "no", "НаПоправку": "no", "Zoon": "no"}}}},
    {{"name": "Верифицированные отзывы", "values": {{"DocDoc": "yes", "НаПоправку": "no", "Zoon": "no"}}}},
    {{"name": "Запись онлайн 24/7", "values": {{"DocDoc": "yes", "НаПоправку": "yes", "Zoon": "частично"}}}},
    {{"name": "Телемедицина", "values": {{"DocDoc": "yes", "НаПоправку": "no", "Zoon": "no"}}}},
    {{"name": "Цена от $5/мес", "values": {{"DocDoc": "no", "НаПоправку": "no", "Zoon": "yes"}}}}
  ]
}}

MARKET SLIDE — поле source в каждой метрике:
"metrics": [
  {{"value": "$140B", "label": "Весь мировой рынок", "trend": "+7%/год", "source": "Grand View Research, 2024. Расчёт: все продажи корма глобально."}},
  {{"value": "$1.8B", "label": "Онлайн СНГ", "trend": "+18%/год", "source": "Data Insight, 2024. ~9% от TAM $20B — средняя e-com пенетрация."}},
  {{"value": "$54M", "label": "Цель за 3 года", "trend": "3% от SAM", "source": "Расчёт: 300К пользователей × $15 × 12 мес. Консервативная оценка."}}
]

СХЕМА JSON:
{schema}
"""

USER_PROMPT_TEMPLATE = Template("""\
Создай презентацию:

Тема: $topic
Тип: $presentation_type
Аудитория: $audience
Язык: $language — ВСЕ тексты только на этом языке
$slide_count_instruction
$extra_instructions_block

Контекст по типу:
$type_context

Контекст по аудитории:
$audience_context

Верни только JSON.
""")

TYPE_CONTEXTS = {
    PresentationType.PITCH_DECK: "Питч для инвесторов. Структура строго по 11 слайдам выше. Акцент на цифрах, рынке, уникальности.",
    PresentationType.DIPLOMA: "Защита дипломной работы. Структура: тема → цель → методология → результаты → выводы. Академический стиль.",
    PresentationType.CORP_REPORT: "Корпоративный отчёт. Структура: резюме → метрики → план vs факт → проблемы → следующий период.",
    PresentationType.EDUCATIONAL: "Обучающая презентация. Структура: введение → концепции → примеры → практика → резюме.",
    PresentationType.SALES: "Коммерческое предложение. Структура: боль → решение → преимущества → кейсы → условия → CTA.",
    PresentationType.CONFERENCE: "Доклад на конференции. Структура: тезис → контекст → ключевые идеи → доказательства → выводы.",
    PresentationType.ROADMAP: "Стратегия и роадмап. Структура: текущее состояние → цели → план по кварталам → ресурсы → результаты.",
}

AUDIENCE_CONTEXTS = {
    AudienceType.INVESTORS: "Инвесторы хотят: рынок, бизнес-модель, тракшн, команду. Язык ROI и масштабируемости. Цифры важнее слов.",
    AudienceType.CLIENTS: "Клиенты хотят: как решает их проблему, каков результат, почему можно доверять. Конкретные кейсы.",
    AudienceType.STUDENTS: "Академическая аудитория. Строгость, методология, логическая последовательность.",
    AudienceType.COLLEAGUES: "Коллеги знают контекст. Фокус на сути, решениях, следующих шагах.",
    AudienceType.MANAGEMENT: "Руководство: краткость и цифры. Выводы сначала, потом детали.",
    AudienceType.GENERAL: "Широкая аудитория. Простой язык, понятные аналогии.",
}


def _get_json_schema() -> str:
    return """{
  "meta": {
    "title": "string", "subtitle": "string|null", "author": "string|null",
    "company": "string|null", "date": "string|null", "language": "ru|en|uz|kk|es|ar|zh|de",
    "presentation_type": "pitch_deck|diploma|corp_report|educational|sales|conference|roadmap",
    "audience": "investors|clients|students|colleagues|management|general",
    "color_scheme": "default"
  },
  "slides": [{
    "index": 1,
    "layout": "title|problem|solution|bullets|market|metrics|two_column|competition|team|timeline|diagram|quote|image_full|closing",
    "title": "string|null", "subtitle": "string|null", "body_text": "string|null",
    "bullets": [{"text": "string", "emphasis": false}],
    "metrics": [{"value": "string", "label": "string", "trend": "string|null", "source": "string|null"}],
    "team_members": [{"name": "string", "role": "string", "bio": "string|null", "gender": "male|female"}],
    "timeline_items": [{"date": "string", "title": "string", "description": "string|null"}],
    "two_column": {"left_title": "string|null", "left_text": "string|null", "left_bullets": [], "right_title": "string|null", "right_text": "string|null", "right_bullets": []},
    "competition_table": {
      "our_name": "string",
      "competitors": [{"name": "string"}],
      "features": [{"name": "string", "values": {"CompetitorName": "yes|no|partial text"}}]
    },
    "image_query": "string|null (ВСЕГДА на английском)",
    "mermaid_code": "string|null",
    "speaker_notes": "string|null"
  }]
}"""


def _build_user_prompt(request: UserRequest) -> str:
    slide_count = request.slide_count_hint or _default_slide_count(request.presentation_type)
    extra_block = ""
    if request.extra_instructions:
        extra_block = f"ВАЖНО — используй эти реальные данные:\n{request.extra_instructions}"
    return USER_PROMPT_TEMPLATE.substitute(
        topic=request.topic,
        presentation_type=request.presentation_type.value,
        audience=request.audience.value,
        language=request.language,
        slide_count_hint=slide_count,
        slide_count_instruction=f"Количество слайдов: {slide_count} (строго).",
        extra_instructions_block=extra_block,
        type_context=TYPE_CONTEXTS.get(request.presentation_type, ""),
        audience_context=AUDIENCE_CONTEXTS.get(request.audience, ""),
    )


def _default_slide_count(presentation_type: PresentationType) -> int:
    defaults = {
        PresentationType.PITCH_DECK:  11,
        PresentationType.DIPLOMA:     12,
        PresentationType.CORP_REPORT: 10,
        PresentationType.EDUCATIONAL: 12,
        PresentationType.SALES:       10,
        PresentationType.CONFERENCE:  10,
        PresentationType.ROADMAP:     11,
    }
    return defaults.get(presentation_type, 10)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def generate_presentation_structure(request: UserRequest) -> PresentationSchema:
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
        max_tokens=4500,
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

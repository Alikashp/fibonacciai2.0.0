import json
import logging
from string import Template

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from schemas.presentation import (
    PresentationSchema, UserRequest, PresentationType, AudienceType,
    ContentVolume, ContentSourceType,
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

ТЕКУЩАЯ ДАТА: {current_date}. Используй её как отправную точку для роадмапа. Все этапы роадмапа — в будущем от этой даты, НЕ в прошлом.

ОБЪЁМ КОНТЕНТА: {volume_instruction}

ДАННЫЕ И ЦИФРЫ:
- Используй реальные данные из своих знаний: рынки, статистику, исследования.
- Источник указывай в поле trend или source: "Grand View Research, 2024".
- Если точных данных нет — используй данные смежной отрасли с пометкой "оценочно".
- НЕ пиши [ЦИФРА] — лучше реалистичный диапазон "$5-15B".

{structure_block}

COMPETITION TABLE — СТРОГИЕ ПРАВИЛА (если структура выше требует layout="competition"):
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
$source_material_block

Контекст по типу:
$type_context

Контекст по аудитории:
$audience_context

Верни только JSON.
""")

SOURCE_MATERIAL_BLOCK = Template("""\
ВАЖНО: используй строго материал ниже как источник фактов. НЕ выдумывай данные и цифры сверх этого текста. Разрешено только добавлять структурные элементы — переходы, заголовки слайдов, логичную последовательность.

МАТЕРИАЛ:
$raw_text""")

TYPE_CONTEXTS = {
    PresentationType.PITCH_DECK: "Питч для инвесторов. Структура строго по 11 слайдам выше. Акцент на цифрах, рынке, уникальности.",
    PresentationType.DIPLOMA: "Защита дипломной работы. Структура: тема → цель → методология → результаты → выводы. Академический стиль.",
    PresentationType.CORP_REPORT: "Корпоративный отчёт. Структура строго по 9 слайдам выше. Резюме → метрики → план vs факт → проблемы → следующий период.",
    PresentationType.EDUCATIONAL: "Обучающая презентация. Структура: введение → концепции → примеры → практика → резюме.",
    PresentationType.SALES: "Коммерческое предложение. Структура: боль → решение → преимущества → кейсы → условия → CTA.",
    PresentationType.CONFERENCE: "Доклад на конференции. Структура строго по 9 слайдам выше. Тезис → контекст → ключевые идеи → доказательства → выводы.",
    PresentationType.ROADMAP: "Стратегия и роадмап. Структура: текущее состояние → цели → план по кварталам → ресурсы → результаты.",
}

# ── Жёсткая по-слайдовая структура для типов, где она уже задана ───────────────
# Для типов, которых здесь нет, используется GENERIC_STRUCTURE_FALLBACK —
# модель ориентируется на TYPE_CONTEXTS выше, но не тянет за собой форму
# питч-дека. Добавляйте сюда новые типы по мере готовности их структуры
# (см. Sprint 1, задача про conference/corp_report).

STRUCTURE_BLOCKS: dict[PresentationType, str] = {

    PresentationType.PITCH_DECK: """\
СТРУКТУРА ПИТЧ-ДЕКА (строго 11 слайдов):
1. title — название, подзаголовок. ОБЯЗАТЕЛЬНО image_query на английском (например: "modern startup office team" или "technology innovation abstract").
2. problem — layout="problem", title = РЕАЛЬНЫЙ заголовок проблемы (не "Проблема"), subtitle = одно предложение. ОБЯЗАТЕЛЬНО: image_query на английском для фото слева. metrics: 2-3 цифры с source.
3. solution — layout="solution", title = РЕАЛЬНЫЙ заголовок решения (не "Решение"), subtitle = одно предложение. ОБЯЗАТЕЛЬНО: image_query для фото справа. bullets: 3 преимущества. metrics: 2-3 результата.
4. why_now — layout="bullets", title = "Почему сейчас?", subtitle = краткий контекст. bullets: 4 тренда, каждый формат "Название — объяснение в 1-2 предложения".
5. market — layout="market", title = "Объём рынка", subtitle = методология расчёта. 3 метрики TAM/SAM/SOM с обязательным полем source (источник + расчёт).
6. biz_model — layout="two_column". title = "Модель монетизации". two_column.left_title = тип модели через запятую (например: "Freemium, Подписка"). left_bullets: СТРОГО массив объектов формата {"text": "Название — Цена", "emphasis": false}. НЕ строки, только объекты. right_text = контекст о модели. БЕЗ юнит-экономики.
7. traction — layout="metrics", title = "Трекшн", subtitle = контекст. 3 метрики прогресса (от прошлого к настоящему).
8. competition — layout="competition". СТРОГО ОБЯЗАТЕЛЬНО заполнить competition_table. Ищи реальных конкурентов: прямых (те же функции), косвенных (альтернативные решения), или крупных игроков рынка. Если конкурентов действительно нет — напиши "Нет прямых конкурентов" в one из ячеек и объясни почему в title. МИНИМУМ 5 критериев сравнения. НИКОГДА не пиши "Competitor A/B/C".
9. team — layout="team", 3-4 члена с gender (male/female), bio обязательно.
10. roadmap — layout="timeline", 4 этапа + body_text с распределением инвестиций по строкам.
11. closing — layout="closing", title = "Давайте работать вместе", subtitle = приглашение к диалогу или вопросы, body_text = условия инвестиций или следующий шаг.""",

    PresentationType.CONFERENCE: """\
СТРУКТУРА ДОКЛАДА НА КОНФЕРЕНЦИИ (строго 9 слайдов):
1. title — layout="title". Тезис доклада: конкретное, спорное или неожиданное утверждение в заголовке, НЕ просто тема ("Мы теряем 40% инженеров на онбординге" вместо "О найме"). ОБЯЗАТЕЛЬНО image_query на английском.
2. context — layout="bullets" (или "quote", если есть сильная цитата/факт-затравка). title = формулировка проблемы/контекста. Почему это важно аудитории именно сейчас — 1 предложение в subtitle.
3. idea1 — layout="bullets". title = формулировка ключевой идеи 1. bullets: 3-4 аргумента.
4. proof1 — layout="metrics" (реальные цифры с source) или "diagram" (mermaid-схема механизма/процесса) — доказательство к идее 1.
5. idea2 — layout="bullets". title = формулировка ключевой идеи 2. bullets: 3-4 аргумента.
6. proof2 — layout="metrics" или "diagram" — доказательство к идее 2 (используй другой вариант, чем в шаге 4, если возможно — не повторяй одинаковый layout дважды подряд без причины).
7. idea3 — layout="bullets" (либо "two_column" для сравнения "было / стало", "до / после"). Третья, обычно самая практичная, идея доклада.
8. takeaway — layout="quote". Главный вывод доклада одной фразой в body_text. subtitle = автор/спикер.
9. closing — layout="closing". Конкретный призыв к действию: что аудитории сделать после доклада (написать, попробовать, задать вопрос в кулуарах).""",

    PresentationType.CORP_REPORT: """\
СТРУКТУРА КОРПОРАТИВНОГО ОТЧЁТА (строго 9 слайдов):
1. title — layout="title". Резюме периода одной строкой в заголовке (например: "Q1 2026: выручка +18%, издержки -6%"), НЕ просто "Отчёт за Q1".
2. kpi — layout="metrics". title = "Ключевые метрики" (или конкретнее). 3 метрики периода, у каждой обязательно trend (рост/падение, знак важен) и source.
3. plan_vs_fact — layout="two_column". two_column.left_title = "План", right_title = "Факт". left/right_bullets — сопоставимые пункты, расхождения называй прямо, без приукрашивания.
4. wins — layout="bullets". title = "Достижения" (или конкретнее). bullets: 3-5 пунктов, что сработало за период.
5. risks — layout="bullets". title = "Проблемы и риски". bullets: 3-5 пунктов, честно и конкретно, без размытых формулировок вроде "есть отдельные сложности".
6. dynamics — layout="metrics" (операционные показатели, воронка) или "diagram" (mermaid-схема процесса/пайплайна). Доказательства к динамике периода.
7. next_period — layout="timeline". 3-4 этапа плана на следующий период с датами.
8. quote — layout="quote". Комментарий руководителя: body_text = прямая цитата, subtitle = имя и должность.
9. closing — layout="closing". Итог периода и конкретные следующие шаги для получателей отчёта.""",

}

GENERIC_STRUCTURE_FALLBACK = (
    "Явной пошаговой структуры (по слайдам) для этого типа презентации пока нет — "
    "ориентируйся на \"Контекст по типу\" в пользовательском сообщении ниже. "
    "Выбирай layout каждого слайда по смыслу его контента "
    "(problem/solution/bullets/market/metrics/two_column/competition/team/timeline/diagram/quote/image_full), "
    "НЕ копируй бездумно форму питч-дека."
)

AUDIENCE_CONTEXTS = {
    AudienceType.INVESTORS: "Инвесторы хотят: рынок, бизнес-модель, тракшн, команду. Язык ROI и масштабируемости. Цифры важнее слов.",
    AudienceType.CLIENTS: "Клиенты хотят: как решает их проблему, каков результат, почему можно доверять. Конкретные кейсы.",
    AudienceType.STUDENTS: "Академическая аудитория. Строгость, методология, логическая последовательность.",
    AudienceType.COLLEAGUES: "Коллеги знают контекст. Фокус на сути, решениях, следующих шагах.",
    AudienceType.MANAGEMENT: "Руководство: краткость и цифры. Выводы сначала, потом детали.",
    AudienceType.GENERAL: "Широкая аудитория. Простой язык, понятные аналогии.",
    AudienceType.SCHOOLKIDS: "Простой язык, без жаргона и канцелярита, аналогии из повседневной жизни, короче предложения.",
    AudienceType.FRIENDS: "Неформальный тон, можно юмор, минимум канцелярита, обращение на 'ты'.",
}

VOLUME_INSTRUCTIONS: dict[ContentVolume, str] = {
    ContentVolume.SHORT: "Кратко. 2-3 bullet-пункта на слайд с bullets, body_text — 1-2 коротких предложения (до 150 символов).",
    ContentVolume.MEDIUM: "Стандартный объём. 4-5 bullet-пунктов на слайд с bullets, body_text — 2-4 предложения (до 350 символов).",
    ContentVolume.LONG: "Развёрнуто. 6 bullet-пунктов на слайд с bullets, body_text — подробный абзац (до 550 символов).",
}


def _get_json_schema() -> str:
    return """{
  "meta": {
    "title": "string", "subtitle": "string|null", "author": "string|null",
    "company": "string|null", "date": "string|null", "language": "ru|en|uz|kk|es|ar|zh|de",
    "presentation_type": "pitch_deck|diploma|corp_report|educational|sales|conference|roadmap",
    "audience": "investors|clients|students|colleagues|management|general|schoolkids|friends",
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
    "two_column": {"left_title": "string|null", "left_text": "string|null", "left_bullets": [{"text": "string", "emphasis": false}], "right_title": "string|null", "right_text": "string|null", "right_bullets": [{"text": "string", "emphasis": false}]},
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

    source_material_block = ""
    if request.source_type != ContentSourceType.TOPIC and request.raw_text:
        source_material_block = SOURCE_MATERIAL_BLOCK.substitute(raw_text=request.raw_text)

    return USER_PROMPT_TEMPLATE.substitute(
        topic=request.topic,
        presentation_type=request.presentation_type.value,
        audience=request.audience.value,
        language=request.language,
        slide_count_hint=slide_count,
        slide_count_instruction=f"Количество слайдов: {slide_count} (строго).",
        extra_instructions_block=extra_block,
        source_material_block=source_material_block,
        type_context=TYPE_CONTEXTS.get(request.presentation_type, ""),
        audience_context=AUDIENCE_CONTEXTS.get(request.audience, ""),
    )


def _default_slide_count(presentation_type: PresentationType) -> int:
    defaults = {
        PresentationType.PITCH_DECK:  11,
        PresentationType.DIPLOMA:     12,
        PresentationType.CORP_REPORT: 9,   # держим в шаге с STRUCTURE_BLOCKS[CORP_REPORT]
        PresentationType.EDUCATIONAL: 12,
        PresentationType.SALES:       10,
        PresentationType.CONFERENCE:  9,   # держим в шаге с STRUCTURE_BLOCKS[CONFERENCE]
        PresentationType.ROADMAP:     11,
    }
    return defaults.get(presentation_type, 10)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def generate_presentation_structure(request: UserRequest) -> PresentationSchema:
    from datetime import datetime
    now = datetime.now()
    months_ru = ["январе","феврале","марте","апреле","мае","июне","июле","августе","сентябре","октябре","ноябре","декабре"]
    current_date = f"Q{(now.month-1)//3+1} {now.year} ({now.day} {months_ru[now.month-1]})"

    system_prompt = SYSTEM_PROMPT.format(
        schema=_get_json_schema(),
        language=request.language,
        current_date=current_date,
        volume_instruction=VOLUME_INSTRUCTIONS.get(request.content_volume, VOLUME_INSTRUCTIONS[ContentVolume.MEDIUM]),
        structure_block=STRUCTURE_BLOCKS.get(request.presentation_type, GENERIC_STRUCTURE_FALLBACK),
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

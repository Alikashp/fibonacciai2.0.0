import json
import logging
from string import Template

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from schemas.presentation import (
    PresentationSchema, UserRequest, PresentationType, AudienceType,
    ContentVolume, ContentSourceType, Slide, SlideLayout, BulletPoint,
    _slide_has_required_content,
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

{competition_table_block}

{market_slide_block}

СХЕМА JSON:
{schema}
"""

# Раньше эти два блока приходили модели ВСЕГДА, при любом presentation_type —
# включая DOKLAD, где ни таблицы конкурентов, ни слайда объёма рынка не
# бывает. Теперь подставляются в SYSTEM_PROMPT только если реально выбранный
# structure_block (см. _pick_structure_block) содержит соответствующий layout
# — см. _competition_table_block()/_market_slide_block() ниже.

COMPETITION_TABLE_BLOCK = """\
COMPETITION TABLE — СТРОГИЕ ПРАВИЛА (если структура выше требует layout="competition"):
1. competitors: ТОЛЬКО реальные названия компаний на этом рынке. ЗАПРЕЩЕНО писать "Competitor A", "Конкурент 1" и любые заглушки.
2. features: МИНИМУМ 5 критериев, релевантных для данной отрасли. Меньше 5 — ошибка.
3. Ключи в values ДОЛЖНЫ точно совпадать с именами в competitors.
4. Значения: "yes", "no", или текст до 10 символов ("частично", "платно").

Пример для медицины:
"competition_table": {
  "our_name": "ЗдоровыйЯ",
  "competitors": [{"name": "DocDoc"}, {"name": "НаПоправку"}, {"name": "Zoon"}],
  "features": [
    {"name": "ИИ-подбор врача", "values": {"DocDoc": "no", "НаПоправку": "no", "Zoon": "no"}},
    {"name": "Верифицированные отзывы", "values": {"DocDoc": "yes", "НаПоправку": "no", "Zoon": "no"}},
    {"name": "Запись онлайн 24/7", "values": {"DocDoc": "yes", "НаПоправку": "yes", "Zoon": "частично"}},
    {"name": "Телемедицина", "values": {"DocDoc": "yes", "НаПоправку": "no", "Zoon": "no"}},
    {"name": "Цена от $5/мес", "values": {"DocDoc": "no", "НаПоправку": "no", "Zoon": "yes"}}
  ]
}"""

MARKET_SLIDE_BLOCK = """\
MARKET SLIDE — поле source в каждой метрике:
"metrics": [
  {"value": "$140B", "label": "Весь мировой рынок", "trend": "+7%/год", "source": "Grand View Research, 2024. Расчёт: все продажи корма глобально."},
  {"value": "$1.8B", "label": "Онлайн СНГ", "trend": "+18%/год", "source": "Data Insight, 2024. ~9% от TAM $20B — средняя e-com пенетрация."},
  {"value": "$54M", "label": "Цель за 3 года", "trend": "3% от SAM", "source": "Расчёт: 300К пользователей × $15 × 12 мес. Консервативная оценка."}
]"""


def _competition_table_block(structure_block: str) -> str:
    return COMPETITION_TABLE_BLOCK if 'layout="competition"' in structure_block else ""


def _market_slide_block(structure_block: str) -> str:
    return MARKET_SLIDE_BLOCK if 'layout="market"' in structure_block else ""

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
    # DOKLAD: реальное значение НЕ читается отсюда — оно веткой считается в
    # _doklad_type_context() внутри _build_user_prompt() в зависимости от
    # request.source_type. Запись здесь — просто документированный дефолт
    # на случай, если кто-то обратится к TYPE_CONTEXTS[DOKLAD] напрямую.
    PresentationType.DOKLAD: "Доклад. Контекст веткой зависит от source_type — см. _doklad_type_context().",
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

# DOKLAD — единственный тип, где даже жёсткая по-слайдовая структура (не
# только TYPE_CONTEXTS) зависит от source_type, а не только от
# presentation_type: "доклад с нуля по теме" и "доклад по проделанной
# работе на основе присланного материала" — это структурно разные вещи
# (см. _pick_structure_block() ниже и template_engine.DOKLAD_TEMPLATE_BY_SOURCE,
# где та же развилка определяет ещё и выбор HTML-шаблона).

LAYOUT_CATALOG = """\
КАТАЛОГ ТИПОВ СЛАЙДОВ — выбирай layout под смысл контента, а не по порядку.

bullets — 3-4 равнозначных пункта. У КАЖДОГО пункта заполни три поля:
  subtitle — название пункта, 1-3 слова;
  text — одно информационное предложение с конкретикой: механизмом, следствием, названием или числом;
  icon — имя иконки из списка ниже, подходящее по смыслу.
  НЕ используй, если пункты образуют последовательность (тогда diagram) или сравниваются попарно (тогда two_column).

metrics — 2-3 показателя. ПРЕДПОЧТИТЕЛЬНО проценты — они рисуются кольцами.
  У каждого: value (например "44%"), label (2-4 слова), trend (короткое пояснение к цифре).
  НЕ используй, если у тебя нет реальных цифр. ЗАПРЕЩЕНО выдумывать числа ради этого layout.

two_column — два параллельных блока по теме слайда. Это может быть:
  • сравнение (до/после, было/стало, в теории/на практике, подход А/подход Б),
  • ИЛИ два самостоятельных тезиса, раскрывающих разные грани одной темы
    (например: «что фиксируем» / «кто отвечает», «внутренние причины» / «внешние причины»).
  Заполни left_title и right_title — название каждого блока по смыслу,
  и по 3 пункта в left_bullets и right_bullets.
  right_preferred=true — ТОЛЬКО если это сравнение и правая сторона объективно предпочтительнее.
  При двух равнозначных тезисах — false.
  НЕ используй, если содержание не делится на две части естественно — тогда бери bullets.

diagram — процесс, механизм, последовательность шагов.
  ОБЯЗАТЕЛЬНО mermaid_code, синтаксис graph LR (горизонтально), МАКСИМУМ 6 узлов.
  Подписи узлов 1-3 слова. body_text — 2-3 предложения, поясняющие логику процесса.
  НЕ используй, если связи между элементами нет.

quote — сильный тезис доклада, поданный крупно.
  title — короткая подпись темы. body_text — сам тезис одним предложением.
  subtitle — одно предложение, поясняющее, почему это так.
  ЗАПРЕЩЕНО приписывать тезис реальным людям или книгам — это формулировка самого докладчика.
  Не больше 1 раза за презентацию.

timeline — этапы во времени. 4 этапа, у каждого date (период), title (2-4 слова), description (одно предложение).

image_full — разбор примера с иллюстрацией. Два сопоставленных блока в two_column-полях
  плюс image_query на английском.

image_hero — когда визуальный образ несёт основную нагрузку.
  ОБЯЗАТЕЛЬНО image_query на английском, title, body_text (2 предложения) и 2 коротких пункта в bullets.

title — только первый слайд. closing — только последний.

ДОПУСТИМЫЕ ИКОНКИ (поле icon, только из этого списка):
file-text, list, check, alert-triangle, clock, calendar, users, user, coin,
chart-bar, target, bulb, search, settings, lock, message-circle, folder,
arrow-up, arrow-right, help-circle, star, bolt, shield, refresh, link

ПРАВИЛА ВЫБОРА:
1. НЕ ставь два слайда с одинаковым layout подряд.
2. НЕ используй один и тот же layout больше 3 раз за презентацию.
3. Стремись использовать не менее 4 разных layout помимо title и closing —
   но только там, где содержание это позволяет.
4. Выбрал layout — заполни ВСЕ его обязательные поля. Слайд с выбранным layout и пустыми полями — грубая ошибка.
5. Поле title заполняется НА КАЖДОМ слайде конкретной содержательной формулировкой по теме.
   "Как модель учится на примерах" — верно.
   "Первая часть темы", "Вторая часть темы", "Ключевые тезисы", "Контекст", пустая строка — ошибка.

ПРИОРИТЕТ ПРАВИЛ: заполняемость важнее разнообразия.
Если для layout нет реального содержания — НЕ выбирай его,
даже если из-за этого разнообразие окажется меньше.
Слайд с выбранным layout и пустыми полями — грубейшая ошибка,
хуже, чем два похожих layout в одной презентации."""

DOKLAD_NARRATIVE_TOPIC = """\
СТРУКТУРА УЧЕБНОГО ДОКЛАДА (строго 9 слайдов):

Слайд 1 — layout="title". Тема простыми словами. ОБЯЗАТЕЛЬНО image_query на английском.
Слайд 8 — выводы: layout="bullets", 3 пункта — что следует из разобранного материала.
Слайд 9 — layout="closing". Заполни только title="Спасибо за внимание".

Слайды 2-7 раскрывают тему в такой смысловой последовательности.
Формулируй заголовок каждого слайда по СОДЕРЖАНИЮ, а не по его роли в структуре.
Layout выбирай САМ по каталогу выше:

2. Где тема проявляется в жизни или практике аудитории.
3. Первая смысловая часть темы — через логику и бытовую аналогию.
4. Наглядный пример или механизм к ней.
5. Вторая смысловая часть темы.
6. Пример, сопоставление или сильная формулировка ко второй части.
7. Что обычно понимают неверно — частые заблуждения.

ВАЖНО: аудитория только знакомится с темой. Объясняй через логику и примеры.
НЕ выдумывай статистику — если реальных данных нет, выбирай layout без чисел.
Тон — докладчика, который разобрался в теме, а не консультанта, который внедрял процесс.
НЕ давай советов формата "начните с...", "через месяц проверьте..." — это доклад, а не консалтинг."""

DOKLAD_NARRATIVE_TEXT = """\
СТРУКТУРА ОТЧЁТНОГО ДОКЛАДА ПО МАТЕРИАЛУ (строго 9 слайдов):

Слайд 1 — layout="title". Резюме работы одной строкой ИЗ материала. ОБЯЗАТЕЛЬНО image_query.
Слайд 8 — выводы: layout="bullets", 3 пункта из материала.
Слайд 9 — layout="closing". Заполни только title="Спасибо за внимание".

Слайды 2-7, layout выбирай САМ по каталогу:

2. Какая была задача или цель — из материала.
3. Что сделано — конкретные шаги СТРОГО из присланного текста.
4. Результаты — если в материале есть цифры, используй metrics; если нет, опиши словами.
5. Дополнительные результаты или описанный в материале процесс.
6. Сложности по ходу работы — честно, без приукрашивания.
7. Главный вывод или наблюдение.

ВАЖНО: используй строго материал из блока МАТЕРИАЛ как источник фактов.
НЕ выдумывай данные, цифры и события сверх присланного текста.
В layout="quote" здесь МОЖНО использовать цитату из материала с указанием источника в subtitle."""

STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE: dict[ContentSourceType, str] = {
    ContentSourceType.TOPIC: LAYOUT_CATALOG + "\n\n" + DOKLAD_NARRATIVE_TOPIC,
    ContentSourceType.TEXT:  LAYOUT_CATALOG + "\n\n" + DOKLAD_NARRATIVE_TEXT,
}
STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE[ContentSourceType.DOCUMENT] = STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE[ContentSourceType.TEXT]
STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE[ContentSourceType.URL] = STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE[ContentSourceType.TEXT]


def _pick_structure_block(request: UserRequest) -> str:
    if request.presentation_type == PresentationType.DOKLAD:
        return STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE.get(
            request.source_type,
            STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE[ContentSourceType.TOPIC],
        )
    return STRUCTURE_BLOCKS.get(request.presentation_type, GENERIC_STRUCTURE_FALLBACK)


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

# Аудитории, для которых DOKLAD+topic-ветка дополнительно усиливает
# требование к простоте языка (сверх того, что уже даёт AUDIENCE_CONTEXTS
# само по себе) — см. _doklad_type_context().
_DOKLAD_SIMPLIFY_AUDIENCES = (AudienceType.SCHOOLKIDS, AudienceType.STUDENTS)


def _doklad_type_context(request: UserRequest) -> str:
    """
    TYPE_CONTEXTS для DOKLAD не статичен — ветка зависит от source_type
    (см. Sprint: "Задача 1. Новый тип DOKLAD с веткой по source_type").
    """
    if request.source_type == ContentSourceType.TOPIC:
        text = (
            "Учебный/ознакомительный доклад. Простое объяснение темы для аудитории, "
            "которая только знакомится с вопросом. Не выдумывай псевдо-исследовательские "
            "цифры без источника — лучше объяснение через логику и примеры, чем фейковая статистика."
        )
        if request.audience in _DOKLAD_SIMPLIFY_AUDIENCES:
            text += (
                " ОСОБЕННО ВАЖНО для этой аудитории: максимально простой язык, короткие "
                "предложения, конкретная бытовая аналогия на каждую идею, никакого термина "
                "без немедленного объяснения простыми словами тут же."
            )
        return text

    # TEXT / DOCUMENT / URL — доклад по уже готовому материалу, не с нуля.
    return (
        "Отчётный доклад по готовому материалу — НЕ учебный и НЕ питч-дек. "
        "Используй строго материал из блока МАТЕРИАЛ ниже как источник фактов, "
        "не выдумывай данные сверх него. Структура отчётная: "
        "что сделано → результаты → сложности → следующие шаги."
    )


VOLUME_INSTRUCTIONS: dict[ContentVolume, str] = {

    ContentVolume.SHORT: """\
ОБЪЁМ КОНТЕНТА — краткий. Ориентиры по типам слайдов:

bullets — 3 пункта. Каждый пункт: одно короткое предложение с конкретикой,
  а не обрывок из 3-4 слов.
  Плохо: "Помогает избежать ошибок".
  Хорошо: "Общий словарь терминов убирает большую часть споров на приёмке".

metrics — 2-3 метрики. label: 2-3 слова. trend: короткая фраза.
  source: одно короткое предложение — откуда цифра.

two_column — по 2-3 пункта в каждой колонке, каждый одним коротким предложением.

diagram — 3-5 узлов в схеме. Подписи узлов: 1-2 слова.
  body_text под схемой: одно предложение.

quote — одно предложение. Отточенная формулировка, не пересказ слайда.

timeline — 3-4 этапа. title этапа: 2-3 слова. description: одно короткое предложение.

image_full — body_text: 1-2 предложения.

closing — body_text: 1-2 предложения, конкретное действие.

Для ЛЮБОГО слайда: subtitle — одно короткое предложение, не повторяющее title.""",

    ContentVolume.MEDIUM: """\
ОБЪЁМ КОНТЕНТА — стандартный. Ориентиры по типам слайдов:

bullets — 4 пункта. Каждый пункт: одно законченное предложение
  формата "Утверждение — объяснение или следствие".
  Плохо: "Помогает избежать недопонимания".
  Хорошо: "Общий словарь терминов убирает большую часть споров на приёмке —
  команда и заказчик перестают понимать одни и те же слова по-разному".

metrics — 3 метрики. label: 2-4 слова. trend: короткая фраза.
  source: одно предложение — откуда цифра и как посчитана.

two_column — по 3 пункта в каждой колонке, каждый одним предложением.

diagram — 4-6 узлов в схеме. Подписи узлов: 1-3 слова.
  body_text под схемой: 1-2 предложения, поясняющие связь между элементами.

quote — 1-2 предложения. Отточенная формулировка, а не пересказ содержания слайда.

timeline — 4 этапа. title этапа: 2-4 слова. description: одно предложение.

image_full — body_text: 2-3 предложения.

closing — body_text: 2-3 предложения, конкретное действие.

Для ЛЮБОГО слайда: subtitle — одно предложение, не повторяющее title.""",

    ContentVolume.LONG: """\
ОБЪЁМ КОНТЕНТА — развёрнутый. Ориентиры по типам слайдов:

bullets — 5 пунктов. Каждый пункт: два предложения — утверждение,
  затем объяснение, пример или следствие.
  Плохо: "Помогает избежать недопонимания".
  Хорошо: "Общий словарь терминов убирает большую часть споров на приёмке.
  Команда и заказчик перестают понимать одни и те же слова по-разному,
  а спорные формулировки всплывают до начала работы, а не после сдачи".

metrics — 3-4 метрики. label: 3-5 слов. trend: фраза с пояснением динамики.
  source: 1-2 предложения — откуда цифра, как посчитана, что из неё следует.

two_column — по 4 пункта в каждой колонке, каждый одним развёрнутым предложением.

diagram — 5-7 узлов в схеме. Подписи узлов: 1-3 слова.
  body_text под схемой: 2-3 предложения, разбирающие логику процесса.

quote — 2-3 предложения. Развёрнутая мысль, а не пересказ содержания слайда.

timeline — 4-5 этапов. title этапа: 2-4 слова. description: 1-2 предложения.

image_full — body_text: 3-4 предложения.

closing — body_text: 3-4 предложения, конкретное действие и следующий шаг.

Для ЛЮБОГО слайда: subtitle — одно развёрнутое предложение, не повторяющее title.""",

}


def _get_json_schema() -> str:
    return """{
  "meta": {
    "title": "string", "subtitle": "string|null", "author": "string|null",
    "company": "string|null", "date": "string|null", "language": "ru|en|uz|kk|es|ar|zh|de",
    "presentation_type": "pitch_deck|diploma|corp_report|educational|sales|conference|roadmap|doklad",
    "audience": "investors|clients|students|colleagues|management|general|schoolkids|friends",
    "color_scheme": "default"
  },
  "slides": [{
    "index": 1,
    "layout": "title|problem|solution|bullets|market|metrics|two_column|competition|team|timeline|diagram|quote|image_full|image_hero|closing",
    "title": "string|null", "subtitle": "string|null", "body_text": "string|null",
    "bullets": [{"text": "string", "subtitle": "string|null", "icon": "string|null (из списка допустимых иконок)", "emphasis": false}],
    "metrics": [{"value": "string", "label": "string", "trend": "string|null", "source": "string|null"}],
    "team_members": [{"name": "string", "role": "string", "bio": "string|null", "gender": "male|female"}],
    "timeline_items": [{"date": "string", "title": "string", "description": "string|null"}],
    "two_column": {"left_title": "string|null", "left_text": "string|null", "left_bullets": [{"text": "string", "emphasis": false}], "right_title": "string|null", "right_text": "string|null", "right_bullets": [{"text": "string", "emphasis": false}], "right_preferred": false},
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

    if request.presentation_type == PresentationType.DOKLAD:
        type_context = _doklad_type_context(request)
    else:
        type_context = TYPE_CONTEXTS.get(request.presentation_type, "")

    return USER_PROMPT_TEMPLATE.substitute(
        topic=request.topic,
        presentation_type=request.presentation_type.value,
        audience=request.audience.value,
        language=request.language,
        slide_count_hint=slide_count,
        slide_count_instruction=f"Количество слайдов: {slide_count} (строго).",
        extra_instructions_block=extra_block,
        source_material_block=source_material_block,
        type_context=type_context,
        audience_context=AUDIENCE_CONTEXTS.get(request.audience, ""),
    )


# ── Точечное дозаполнение пустых слайдов ────────────────────────────────────
# Наблюдали в проде трижды подряд на одной и той же теме: модель ставит
# layout на слайд (чаще всего two_column), но не заполняет поле, которое
# этот layout реально рисует — слайд визуально пустой, кроме футера.
# Жёсткая валидация с retry (пробовали раньше) оказалась хуже: модель может
# СТАБИЛЬНО повторять один и тот же пустой слайд на всех 3 попытках подряд,
# и пользователь не получает вообще ничего. Вместо этого — один маленький
# точечный follow-up запрос ТОЛЬКО на недостающее поле конкретного слайда:
# дешевле и надёжнее полной перегенерации, а если и он не удастся —
# оставляем слайд как есть (тихо, без падения job'а).

_LAYOUT_PATCH_INSTRUCTIONS: dict[SlideLayout, str] = {
    SlideLayout.BULLETS: (
        'Заполни "bullets": 3-4 пункта. У каждого: "text" — информационное '
        'предложение с конкретикой, "subtitle" — название пункта (1-3 слова), '
        '"icon" — одна из: file-text, list, check, alert-triangle, clock, calendar, '
        'users, user, coin, chart-bar, target, bulb, search, settings, lock, '
        'message-circle, folder, arrow-up, arrow-right, help-circle, star, bolt, '
        'shield, refresh, link.'
    ),
    SlideLayout.WHY_NOW: (
        'Заполни "bullets": 3-4 пункта, каждый — одно законченное предложение.'
    ),
    SlideLayout.METRICS: (
        'Заполни "metrics": 2-3 показателя. У каждого: "value" (например "44%"), '
        '"label" (2-4 слова), "trend" (короткое пояснение к цифре). '
        'Используй правдоподобные для темы цифры, не выдумывай абсурдные значения.'
    ),
    SlideLayout.TWO_COLUMN: (
        'Заполни "two_column": "left_title", "right_title" (название каждой '
        'стороны сравнения) и по 3 пункта в "left_bullets"/"right_bullets" — '
        'каждый пункт объект {"text": "..."}.'
    ),
    SlideLayout.DIAGRAM: (
        'Заполни "mermaid_code" — валидный Mermaid, синтаксис "graph LR", '
        'максимум 6 узлов, подписи узлов 1-3 слова. И "body_text" — '
        '2-3 предложения, поясняющие логику процесса.'
    ),
    SlideLayout.QUOTE: (
        'Заполни "body_text" — сильный тезис одним предложением. '
        '"subtitle" — одно предложение, поясняющее, почему это так.'
    ),
    SlideLayout.TIMELINE: (
        'Заполни "timeline_items": 4 этапа. У каждого: "date" (период), '
        '"title" (2-4 слова), "description" (одно предложение).'
    ),
    SlideLayout.IMAGE_FULL: (
        'Заполни "image_query" (на английском, для поиска фото) и "two_column" '
        'с двумя сопоставленными блоками: "left_title"/"left_text" и '
        '"right_title"/"right_text".'
    ),
    SlideLayout.IMAGE_HERO: (
        'Заполни "image_query" (на английском), "body_text" (2 предложения) и '
        '"bullets" — 2 коротких пункта, каждый объект {"text": "..."}.'
    ),
    SlideLayout.TEAM: (
        'Заполни "team_members": 3-4 члена команды, у каждого "name", "role", '
        '"gender" (male/female).'
    ),
    SlideLayout.COMPETITION: (
        'Заполни "competition_table": "our_name", "competitors" (реальные '
        'названия компаний), "features" (минимум 5 критериев сравнения).'
    ),
}

_PATCH_JSON_EXAMPLES: dict[SlideLayout, str] = {
    SlideLayout.BULLETS: '{"bullets": [{"text": "...", "subtitle": "...", "icon": "..."}]}',
    SlideLayout.WHY_NOW: '{"bullets": [{"text": "..."}]}',
    SlideLayout.METRICS: '{"metrics": [{"value": "...", "label": "...", "trend": "..."}]}',
    SlideLayout.TWO_COLUMN: '{"two_column": {"left_title": "...", "right_title": "...", "left_bullets": [{"text": "..."}], "right_bullets": [{"text": "..."}]}}',
    SlideLayout.DIAGRAM: '{"mermaid_code": "graph LR\\nA[...] --> B[...]", "body_text": "..."}',
    SlideLayout.QUOTE: '{"body_text": "...", "subtitle": "..."}',
    SlideLayout.TIMELINE: '{"timeline_items": [{"date": "...", "title": "...", "description": "..."}]}',
    SlideLayout.IMAGE_FULL: '{"image_query": "...", "two_column": {"left_title": "...", "left_text": "...", "right_title": "...", "right_text": "..."}}',
    SlideLayout.IMAGE_HERO: '{"image_query": "...", "body_text": "...", "bullets": [{"text": "..."}]}',
    SlideLayout.TEAM: '{"team_members": [{"name": "...", "role": "...", "gender": "male"}]}',
    SlideLayout.COMPETITION: '{"competition_table": {"our_name": "...", "competitors": [{"name": "..."}], "features": [{"name": "...", "values": {}}]}}',
}


# Layout'ы, для которых есть дешёвый локальный путь деградации в bullets
# ДО обращения к LLM — просто перекладываем то, что реально пришло в других
# полях, без выдумывания нового текста. Дешевле и быстрее второго вызова
# модели; используется, только если исходный layout всё равно не прошёл
# _slide_has_required_content (т.е. слайд и так уже "сломан").
_DEGRADABLE_TO_BULLETS = (
    SlideLayout.TWO_COLUMN, SlideLayout.METRICS, SlideLayout.DIAGRAM,
    SlideLayout.IMAGE_HERO, SlideLayout.TIMELINE,
)


def _locally_degrade_slide(slide: Slide) -> Slide | None:
    if slide.layout not in _DEGRADABLE_TO_BULLETS:
        return None

    bullets = list(slide.bullets)

    if slide.layout == SlideLayout.TWO_COLUMN and slide.two_column:
        tc = slide.two_column
        bullets.extend(tc.left_bullets)
        bullets.extend(tc.right_bullets)
        if tc.left_text and tc.left_text.strip():
            bullets.append(BulletPoint(text=tc.left_text.strip(), subtitle=tc.left_title))
        if tc.right_text and tc.right_text.strip():
            bullets.append(BulletPoint(text=tc.right_text.strip(), subtitle=tc.right_title))

    # metrics/diagram/image_hero/timeline не содержат других текстовых полей,
    # из которых можно честно собрать bullet — просто меняем layout на
    # bullets, и если реально нечего сливать, ниже это увидит
    # _slide_has_required_content и уйдёт на LLM-дозапрос уже как bullets.

    degraded = slide.model_copy(update={"layout": SlideLayout.BULLETS, "bullets": bullets})
    return degraded


async def _patch_one_slide(request: UserRequest, slide: Slide) -> Slide:
    instruction = _LAYOUT_PATCH_INSTRUCTIONS.get(slide.layout)
    example = _PATCH_JSON_EXAMPLES.get(slide.layout)
    if instruction is None or example is None:
        raise ValueError(f"no patch instructions for layout={slide.layout.value}")

    material_block = ""
    if request.source_type != ContentSourceType.TOPIC and request.raw_text:
        material_block = f'\nМАТЕРИАЛ (используй как источник фактов):\n{request.raw_text[:3000]}'

    system_prompt = f"""\
Ты дописываешь ОДИН слайд презентации, у которого не хватает содержания.
Возвращай ТОЛЬКО валидный JSON без пояснений и markdown.

Тема презентации: {request.topic}
Аудитория: {request.audience.value}
Язык: {request.language} — весь текст только на этом языке.{material_block}

Этот слайд имеет layout="{slide.layout.value}"{f', заголовок "{slide.title}"' if slide.title else ""}{f', подзаголовок "{slide.subtitle}"' if slide.subtitle else ""}.
{instruction}

Верни JSON строго такой формы (реальный осмысленный контент по теме, не заглушки):
{example}"""

    response = await client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.7,
        max_tokens=800,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system_prompt}],
    )
    data = json.loads(response.choices[0].message.content)

    merged = slide.model_dump(mode="json")
    merged.update({k: v for k, v in data.items() if k in Slide.model_fields})
    patched = Slide.model_validate(merged)
    patched.index = slide.index
    patched.layout = slide.layout
    return patched


async def _patch_empty_slides(request: UserRequest, presentation: PresentationSchema) -> PresentationSchema:
    gaps = [
        s for s in presentation.slides
        if s.layout not in (SlideLayout.TITLE, SlideLayout.CLOSING)
        and not _slide_has_required_content(s)
    ]
    if not gaps:
        return presentation

    slides = list(presentation.slides)
    for slide in gaps:
        # Сначала дешёвая локальная деградация (без вызова LLM) — только
        # перекладывает то, что реально уже пришло в других полях слайда.
        # Если этого хватило — точечный запрос к модели не нужен вообще.
        degraded = _locally_degrade_slide(slide)
        working = degraded if degraded is not None else slide

        if degraded is not None and _slide_has_required_content(working):
            slides[slide.index - 1] = working
            logger.info(
                f"Locally degraded slide #{slide.index} from layout={slide.layout.value} "
                f"to bullets without an extra LLM call — salvaged {len(working.bullets)} bullet(s)"
            )
            continue

        try:
            patched = await _patch_one_slide(request, working)
            if _slide_has_required_content(patched):
                slides[slide.index - 1] = patched
                logger.info(
                    f"Patched empty slide #{slide.index} (layout={patched.layout.value}, "
                    f"originally {slide.layout.value})"
                )
            else:
                logger.warning(
                    f"Patch attempt for slide #{slide.index} (layout={patched.layout.value}, "
                    f"originally {slide.layout.value}) still came back without the required "
                    f"content — leaving as-is"
                )
        except Exception as e:
            logger.warning(
                f"Failed to patch empty slide #{slide.index} (layout={working.layout.value}, "
                f"originally {slide.layout.value}): {e}"
            )

    return presentation.model_copy(update={"slides": slides})


def _default_slide_count(presentation_type: PresentationType) -> int:
    defaults = {
        PresentationType.PITCH_DECK:  11,
        PresentationType.DIPLOMA:     12,
        PresentationType.CORP_REPORT: 9,   # держим в шаге с STRUCTURE_BLOCKS[CORP_REPORT]
        PresentationType.EDUCATIONAL: 12,
        PresentationType.SALES:       10,
        PresentationType.CONFERENCE:  9,   # держим в шаге с STRUCTURE_BLOCKS[CONFERENCE]
        PresentationType.ROADMAP:     11,
        PresentationType.DOKLAD:      9,   # держим в шаге со STRUCTURE_BLOCKS_DOKLAD_BY_SOURCE
    }
    return defaults.get(presentation_type, 10)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def generate_presentation_structure(request: UserRequest) -> PresentationSchema:
    from datetime import datetime
    now = datetime.now()
    months_ru = ["январе","феврале","марте","апреле","мае","июне","июле","августе","сентябре","октябре","ноябре","декабре"]
    current_date = f"Q{(now.month-1)//3+1} {now.year} ({now.day} {months_ru[now.month-1]})"

    structure_block = _pick_structure_block(request)
    system_prompt = SYSTEM_PROMPT.format(
        schema=_get_json_schema(),
        language=request.language,
        current_date=current_date,
        volume_instruction=VOLUME_INSTRUCTIONS.get(request.content_volume, VOLUME_INSTRUCTIONS[ContentVolume.MEDIUM]),
        structure_block=structure_block,
        competition_table_block=_competition_table_block(structure_block),
        market_slide_block=_market_slide_block(structure_block),
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
        # 4500 хватало на старую структуру, но DOKLAD теперь пишет subtitle+icon
        # на КАЖДЫЙ bullet и лимиты полей выросли (bullets/metrics.source и
        # т.д.) — в json_object-режиме модель при нехватке бюджета не ломает
        # синтаксис, а тихо обрезает слайды/поля в конце, чтобы закрыть JSON
        # корректно. Из-за этого "строго 9 слайдов" превращалось в 8 без
        # ошибки парсинга — см. лог с slide_count=8.
        max_tokens=8000,
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

    presentation = await _patch_empty_slides(request, presentation)

    return presentation

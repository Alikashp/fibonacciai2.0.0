"""
Диагностика свободного выбора layout для DOKLAD (после перехода на
LAYOUT_CATALOG) — 5 сценариев из критерия готовности:

1. техническая тема с доступной статистикой
2. гуманитарная тема без цифр
3. тема про процесс (где напрашивается diagram)
4. source_type=TEXT с реальным куском текста, где есть цифры
5. source_type=TEXT с текстом без цифр

Для каждого сценария печатает:
  - [s.layout for s in slides] — последовательность layout
  - [s.title for s in slides] — проверка, что нет пустых/None
  - количество слов в каждом bullet (чтобы отличить предложение от обрывка)

Нужен рабочий OPENAI_API_KEY/OPENAI_BASE_URL в окружении — реальный вызов
LLM, ничего не мокается.

Запуск (из корня репозитория):
    cd src && python3 ../scripts/diagnose_doklad_layouts.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from schemas.presentation import (
    UserRequest, PresentationType, AudienceType, ContentSourceType, ContentVolume,
)
from generation.llm import generate_presentation_structure

TEXT_WITH_NUMBERS = """\
За квартал стажировки мы переписали модуль оплаты на новый провайдер.
Время обработки платежа сократилось с 4.2 секунды до 1.1 секунды.
Количество отклонённых транзакций упало с 6.8% до 2.3%.
Основная сложность — миграция 40 000 сохранённых карт без потери данных,
заняла три недели вместо запланированной одной. В следующем квартале
планируем добавить поддержку Apple Pay и провести нагрузочное тестирование
на пиковую нагрузку Чёрной пятницы."""

TEXT_WITHOUT_NUMBERS = """\
Мы провели редизайн раздела помощи в приложении. Раньше пользователи
жаловались, что не могут найти ответ на простой вопрос и вместо этого
писали в поддержку. Мы переработали структуру статей, сгруппировали их
по сценариям использования вместо разделов по функциям продукта и
добавили поиск с подсказками. Основная сложность была в том, чтобы
договориться с командой поддержки, какие вопросы вообще стоит выносить
в самопомощь, а какие требуют живого разговора с человеком. Следующий шаг —
собрать обратную связь от пользователей и решить, нужен ли чат-бот
на первой линии."""

SCENARIOS = [
    dict(
        name="1. Техническая тема со статистикой",
        topic="Как работают нейросети и почему они ошибаются",
        source_type=ContentSourceType.TOPIC,
        raw_text=None,
    ),
    dict(
        name="2. Гуманитарная тема без цифр",
        topic="Почему люди верят в приметы",
        source_type=ContentSourceType.TOPIC,
        raw_text=None,
    ),
    dict(
        name="3. Тема про процесс (напрашивается diagram)",
        topic="Как устроен процесс регистрации нового пользователя в приложении",
        source_type=ContentSourceType.TOPIC,
        raw_text=None,
    ),
    dict(
        name="4. source_type=TEXT, есть цифры",
        topic="Итоги квартала: миграция платёжного модуля",
        source_type=ContentSourceType.TEXT,
        raw_text=TEXT_WITH_NUMBERS,
    ),
    dict(
        name="5. source_type=TEXT, без цифр",
        topic="Редизайн раздела помощи в приложении",
        source_type=ContentSourceType.TEXT,
        raw_text=TEXT_WITHOUT_NUMBERS,
    ),
]


def word_count(text: str) -> int:
    return len(text.split())


async def run_scenario(scenario: dict) -> None:
    print(f"\n{'=' * 70}")
    print(scenario["name"])
    print(f"Тема: {scenario['topic']}")
    print(f"{'=' * 70}")

    request = UserRequest(
        topic=scenario["topic"],
        presentation_type=PresentationType.DOKLAD,
        audience=AudienceType.GENERAL,
        source_type=scenario["source_type"],
        raw_text=scenario["raw_text"],
        content_volume=ContentVolume.MEDIUM,
    )

    try:
        presentation = await generate_presentation_structure(request)
    except Exception as e:
        print(f"ОШИБКА генерации: {e}")
        return

    layouts = [s.layout.value for s in presentation.slides]
    titles = [s.title for s in presentation.slides]
    empty_titles = [i + 1 for i, t in enumerate(titles) if not t or not t.strip()]

    print(f"\nlayouts: {layouts}")
    print(f"titles: {titles}")
    if empty_titles:
        print(f"!!! ПУСТЫЕ TITLE на слайдах: {empty_titles}")
    else:
        print("titles: все заполнены")

    distinct = set(layouts) - {"title", "closing"}
    print(f"различных layout (кроме title/closing): {len(distinct)} -> {sorted(distinct)}")

    consecutive_repeats = [
        (i + 1, layouts[i]) for i in range(len(layouts) - 1) if layouts[i] == layouts[i + 1]
    ]
    if consecutive_repeats:
        print(f"!!! ПОДРЯД ОДИНАКОВЫЕ layout: {consecutive_repeats}")

    print("\nbullets (кол-во слов на пункт):")
    for slide in presentation.slides:
        if slide.bullets:
            counts = [word_count(b.text) for b in slide.bullets]
            short = [c for c in counts if c <= 4]
            flag = f"  <-- {len(short)} из {len(counts)} похожи на обрывок (<=4 слов)" if short else ""
            print(f"  [{slide.index}] {slide.layout.value}: {counts} слов{flag}")
            for b in slide.bullets:
                print(f"      ({word_count(b.text)} слов) {b.text!r}")


async def main() -> None:
    for scenario in SCENARIOS:
        await run_scenario(scenario)


if __name__ == "__main__":
    asyncio.run(main())

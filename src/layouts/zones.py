"""
Реестр зон компоновки для каждого SlideLayout.

Координаты (x, y, w, h в px, холст 794×446) выведены из реальных CSS-правил
в templates/pitch_deck/template.html (padding контейнеров, фиксированные
ширины фото-блоков, flex/grid-области). Это справочная геометрия — она не
подключена к рендеру pitch_deck (тот работает на своих отточенных
flexbox/grid CSS-классах и трогать его вёрстку в рамках этой задачи мы не
стали, чтобы не потерять визуальный результат без прогона скриншот-диффа).

Новые шаблоны (conference, corp_report, см. templates/conference,
templates/corp_report) уже используют эти зоны через inline-style, как и
требует архитектура: один источник координат — разные потребители
(сегодня Playwright/CSS, в будущем — python-pptx).
"""

from schemas.layout import Zone, SlideLayoutZones
from schemas.presentation import SlideLayout

ZONES_REGISTRY: dict[SlideLayout, SlideLayoutZones] = {

    SlideLayout.TITLE: SlideLayoutZones(
        layout=SlideLayout.TITLE,
        zones={
            "eyebrow":  Zone(x=22,  y=46,  w=300, h=14),
            "title":    Zone(x=22,  y=62,  w=412, h=120),
            "subtitle": Zone(x=22,  y=186, w=280, h=60),
            "image":    Zone(x=492, y=0,   w=302, h=446),
            "meta":     Zone(x=22,  y=402, w=460, h=30),
        },
    ),

    SlideLayout.PROBLEM: SlideLayoutZones(
        layout=SlideLayout.PROBLEM,
        zones={
            "image":    Zone(x=0,   y=0,  w=280, h=446),
            "title":    Zone(x=300, y=16, w=474, h=40),
            "subtitle": Zone(x=300, y=60, w=474, h=30),
            "metrics":  Zone(x=300, y=100, w=474, h=300),
        },
    ),

    SlideLayout.SOLUTION: SlideLayoutZones(
        layout=SlideLayout.SOLUTION,
        zones={
            "title":    Zone(x=22,  y=18, w=510, h=50),
            "subtitle": Zone(x=22,  y=74, w=510, h=40),
            "bullets":  Zone(x=22,  y=120, w=510, h=290),
            "image":    Zone(x=554, y=0,  w=240, h=446),
        },
    ),

    SlideLayout.MARKET: SlideLayoutZones(
        layout=SlideLayout.MARKET,
        zones={
            "title":    Zone(x=20, y=16, w=754, h=40),
            "subtitle": Zone(x=20, y=54, w=754, h=20),
            "metrics":  Zone(x=20, y=90, w=754, h=328),
        },
    ),

    SlideLayout.METRICS: SlideLayoutZones(
        layout=SlideLayout.METRICS,
        zones={
            "title":    Zone(x=20,  y=16, w=500, h=36),
            "subtitle": Zone(x=540, y=16, w=234, h=36),
            "metrics":  Zone(x=20,  y=68, w=754, h=350),
        },
    ),

    SlideLayout.BULLETS: SlideLayoutZones(
        layout=SlideLayout.BULLETS,
        zones={
            "title":    Zone(x=20,  y=16, w=300, h=60),
            "subtitle": Zone(x=400, y=16, w=374, h=40),
            "bullets":  Zone(x=20,  y=90, w=754, h=328),
        },
    ),

    SlideLayout.TWO_COLUMN: SlideLayoutZones(
        layout=SlideLayout.TWO_COLUMN,
        zones={
            "title": Zone(x=22,  y=24, w=750, h=40),
            "left":  Zone(x=22,  y=90, w=360, h=310),
            "right": Zone(x=406, y=90, w=366, h=310),
        },
    ),

    SlideLayout.COMPETITION: SlideLayoutZones(
        layout=SlideLayout.COMPETITION,
        zones={
            "title": Zone(x=18, y=14, w=758, h=40),
            "table": Zone(x=18, y=60, w=758, h=358),
        },
    ),

    SlideLayout.TEAM: SlideLayoutZones(
        layout=SlideLayout.TEAM,
        zones={
            "title": Zone(x=18, y=14, w=758, h=40),
            "team":  Zone(x=18, y=60, w=758, h=358),
        },
    ),

    SlideLayout.TIMELINE: SlideLayoutZones(
        layout=SlideLayout.TIMELINE,
        zones={
            "title":        Zone(x=20, y=16,  w=754, h=36),
            "timeline":     Zone(x=20, y=68,  w=754, h=180),
            "footer_bars":  Zone(x=20, y=264, w=754, h=138),
        },
    ),

    SlideLayout.DIAGRAM: SlideLayoutZones(
        layout=SlideLayout.DIAGRAM,
        zones={
            "title":   Zone(x=20,  y=18, w=210, h=40),
            "body":    Zone(x=20,  y=70, w=210, h=340),
            "diagram": Zone(x=250, y=0,  w=544, h=446),
        },
    ),

    SlideLayout.QUOTE: SlideLayoutZones(
        layout=SlideLayout.QUOTE,
        zones={
            "quote_mark": Zone(x=297, y=60,  w=200, h=60),
            "text":       Zone(x=147, y=140, w=500, h=140),
            "author":     Zone(x=147, y=290, w=500, h=30),
        },
    ),

    SlideLayout.IMAGE_FULL: SlideLayoutZones(
        layout=SlideLayout.IMAGE_FULL,
        zones={
            "image":    Zone(x=0,  y=0,   w=794, h=446),
            "title":    Zone(x=24, y=340, w=700, h=40),
            "subtitle": Zone(x=24, y=384, w=700, h=30),
        },
    ),

    SlideLayout.CLOSING: SlideLayoutZones(
        layout=SlideLayout.CLOSING,
        zones={
            "title":    Zone(x=147, y=140, w=500, h=30),
            "subtitle": Zone(x=97,  y=180, w=600, h=60),
            "body":     Zone(x=197, y=250, w=400, h=60),
            "contacts": Zone(x=147, y=396, w=500, h=30),
        },
    ),

    # WHY_NOW переиспользует ту же геометрию, что и BULLETS —
    # в текущей схеме это тот же layout="bullets" (см. схемы presentation.py),
    # но enum содержит отдельное значение WHY_NOW про запас на будущее.
    SlideLayout.WHY_NOW: SlideLayoutZones(
        layout=SlideLayout.WHY_NOW,
        zones={
            "title":    Zone(x=20,  y=16, w=300, h=60),
            "subtitle": Zone(x=400, y=16, w=374, h=40),
            "bullets":  Zone(x=20,  y=90, w=754, h=328),
        },
    ),
}


def get_zones(layout: SlideLayout) -> SlideLayoutZones:
    return ZONES_REGISTRY[layout]

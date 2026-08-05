"""
Zone-based модель компоновки слайда.

Это единый источник координат элементов слайда. Сегодня его читает только
HTML/CSS-рендерер (через inline-style в Jinja-шаблонах), но модель
сознательно не завязана на CSS — те же Zone координаты (x, y, w, h в px
на холсте 794×446, см. generation/pdf_renderer.py PAGE_WIDTH_PX/PAGE_HEIGHT_PX)
в будущем сможет читать python-pptx рендерер без изменения схемы.
"""

from pydantic import BaseModel, Field

from schemas.presentation import SlideLayout

# Базовый холст слайда — совпадает с PAGE_WIDTH_PX / PAGE_HEIGHT_PX в pdf_renderer.py
CANVAS_W = 794
CANVAS_H = 446


class Zone(BaseModel):
    x: int = Field(..., ge=0, le=CANVAS_W)
    y: int = Field(..., ge=0, le=CANVAS_H)
    w: int = Field(..., gt=0, le=CANVAS_W)
    h: int = Field(..., gt=0, le=CANVAS_H)

    def as_style(self) -> str:
        """Готовая inline-style строка для Jinja: style="{{ zone.as_style() }}" """
        return f"left:{self.x}px;top:{self.y}px;width:{self.w}px;height:{self.h}px;"


class SlideLayoutZones(BaseModel):
    layout: SlideLayout
    zones: dict[str, Zone]

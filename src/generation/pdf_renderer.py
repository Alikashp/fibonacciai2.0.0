"""
PDF рендерер на Playwright.

Архитектурные решения:
1. Один browser instance на весь воркер — не запускаем Chromium на каждый запрос.
   Это экономит ~2-3 секунды холодного старта и ~300MB RAM.
2. Каждый рендер — отдельный BrowserContext + Page, изолированно.
   Контекст закрывается после рендера — нет утечек памяти.
3. Mermaid ждём через waitForFunction — надёжнее чем networkidle или sleep.
4. HTML передаём через data URL, а не temp-файл — меньше I/O.
5. Таймаут на весь рендер: 30 секунд. Если Mermaid завис — не блокируем воркер.
"""

import asyncio
import base64
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
    Error as PlaywrightError,
)

logger = logging.getLogger(__name__)

# A4 landscape в пикселях при 96 DPI — совпадает с нашим CSS --slide-w/h
PAGE_WIDTH_PX  = 794
PAGE_HEIGHT_PX = 446  # per slide, но PDF пересчитывает сам

# Playwright PDF использует CSS px → pt конверсию: 1px = 0.75pt
# A4 landscape: 297mm × 210mm
PDF_WIDTH  = "297mm"
PDF_HEIGHT = "210mm"

RENDER_TIMEOUT_MS = 30_000   # 30 секунд на весь рендер
MERMAID_TIMEOUT_MS = 10_000  # 10 секунд ждём Mermaid


class PDFRenderer:
    """
    Синглтон-рендерер. Держит один Browser на всё время жизни воркера.

    Использование:
        renderer = PDFRenderer()
        await renderer.start()
        pdf_bytes = await renderer.render(html)
        await renderer.stop()

    Или через контекстный менеджер:
        async with PDFRenderer.create() as renderer:
            pdf_bytes = await renderer.render(html)
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()  # Защита от race condition при старте

    async def start(self) -> None:
        """Запускает Chromium. Вызывать один раз при старте воркера."""
        async with self._lock:
            if self._browser is not None:
                return  # Уже запущен

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                args=[
                    "--no-sandbox",                    # Обязательно в Docker
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",         # /dev/shm мал в контейнерах
                    "--disable-gpu",                   # GPU не нужен для PDF
                    "--disable-web-security",          # Для data: URL с внешними ресурсами
                    "--font-render-hinting=none",      # Стабильный рендер шрифтов
                ],
                headless=True,
            )
            logger.info("Playwright Chromium started")

    async def stop(self) -> None:
        """Останавливает Chromium. Вызывать при shutdown воркера."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright Chromium stopped")

    @asynccontextmanager
    async def _page_context(self) -> AsyncGenerator[Page, None]:
        """
        Создаёт изолированный BrowserContext + Page.
        Гарантирует закрытие контекста даже при исключении.
        """
        if self._browser is None:
            raise RuntimeError("PDFRenderer not started. Call await renderer.start() first.")

        context: BrowserContext = await self._browser.new_context(
            viewport={"width": PAGE_WIDTH_PX, "height": PAGE_HEIGHT_PX},
            device_scale_factor=2,  # Retina — чёткий текст в PDF
        )
        page: Page = await context.new_page()

        try:
            yield page
        finally:
            await context.close()

    async def render(self, html: str, has_mermaid: bool = False) -> bytes:
        """
        Рендерит HTML в PDF.

        Args:
            html:         Полный HTML-документ (из template_engine.render_presentation)
            has_mermaid:  True если есть diagram-слайды — ждём Mermaid.js

        Returns:
            PDF как bytes

        Raises:
            PlaywrightError: При таймауте или ошибке рендера
            RuntimeError:    Если renderer не запущен
        """
        start_time = time.monotonic()

        async with self._page_context() as page:
            # Устанавливаем таймаут на всю операцию
            page.set_default_timeout(RENDER_TIMEOUT_MS)

            # Загружаем HTML через data URL
            # Альтернатива — temp file через file://, но data URL проще и надёжнее
            encoded = base64.b64encode(html.encode("utf-8")).decode("ascii")
            data_url = f"data:text/html;base64,{encoded}"

            await page.goto(data_url, wait_until="domcontentloaded")

            # Ждём загрузки Google Fonts
            # Используем networkidle с коротким таймаутом — не блокируем если fonts недоступны
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightError:
                # Fonts не загрузились — fallback на системные, продолжаем
                logger.warning("Network idle timeout — proceeding with system fonts")

            # Ждём Mermaid.js если есть диаграммы
            if has_mermaid:
                await self._wait_for_mermaid(page)

# Генерируем PDF
            pdf_bytes = await page.pdf(
                width="794px",
                height="446px",
                print_background=True,
                margin={
                    "top": "0",
                    "right": "0",
                    "bottom": "0",
                    "left": "0",
                },
            )

        elapsed = time.monotonic() - start_time
        logger.info(
            "PDF rendered",
            extra={
                "size_kb": len(pdf_bytes) // 1024,
                "elapsed_sec": round(elapsed, 2),
                "has_mermaid": has_mermaid,
            },
        )

        return pdf_bytes

    async def _wait_for_mermaid(self, page: Page) -> None:
        """
        Ждёт пока Mermaid.js отрендерит все диаграммы.

        Стратегия: ждём пока все .mermaid div перестанут содержать
        исходный текст и будут содержать SVG.

        Это надёжнее чем:
        - sleep(N) — непредсказуемо
        - networkidle — Mermaid рендерит синхронно, не делает запросов
        - waitForSelector('.mermaid svg') — может не появиться если Mermaid упал
        """
        try:
            await page.wait_for_function(
                """() => {
                    const diagrams = document.querySelectorAll('.mermaid');
                    if (diagrams.length === 0) return true;
                    return Array.from(diagrams).every(el => {
                        // Mermaid заменяет текст на SVG
                        return el.querySelector('svg') !== null;
                    });
                }""",
                timeout=MERMAID_TIMEOUT_MS,
            )
            logger.debug("Mermaid diagrams rendered")
        except PlaywrightError as e:
            # Mermaid не отрендерился — продолжаем без диаграмм
            # Лучше PDF без диаграммы, чем нет PDF вовсе
            logger.warning(f"Mermaid render timeout: {e}. Proceeding without diagrams.")

    @classmethod
    @asynccontextmanager
    async def create(cls) -> AsyncGenerator["PDFRenderer", None]:
        """Контекстный менеджер для использования в тестах и скриптах."""
        renderer = cls()
        await renderer.start()
        try:
            yield renderer
        finally:
            await renderer.stop()


# ── Глобальный синглтон для воркера ───────────────────────────────────────────
# Инициализируется один раз при старте ARQ воркера через lifespan

_renderer: PDFRenderer | None = None


async def get_renderer() -> PDFRenderer:
    """Возвращает глобальный рендерер. Запускает если не запущен."""
    global _renderer
    if _renderer is None:
        _renderer = PDFRenderer()
        await _renderer.start()
    return _renderer


async def shutdown_renderer() -> None:
    """Останавливает глобальный рендерер. Вызывать при shutdown воркера."""
    global _renderer
    if _renderer is not None:
        await _renderer.stop()
        _renderer = None


# ── Вспомогательная функция — основная точка входа из воркера ─────────────────

async def html_to_pdf(
    html: str,
    has_mermaid: bool = False,
    output_path: Path | None = None,
) -> bytes:
    """
    Главная функция: HTML → PDF bytes.

    Args:
        html:         HTML от template_engine
        has_mermaid:  Есть ли diagram-слайды
        output_path:  Если задан — сохраняет PDF на диск (для дебага)

    Returns:
        PDF как bytes (для записи в S3)
    """
    renderer = await get_renderer()
    pdf_bytes = await renderer.render(html, has_mermaid=has_mermaid)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(pdf_bytes)
        logger.info(f"PDF saved to {output_path}")

    return pdf_bytes

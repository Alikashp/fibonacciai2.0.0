FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Зависимости отдельным слоем — кешируется при изменении только кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright браузеры уже в образе — не скачиваем
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Исходники
COPY src/ ./src/

# Запуск
CMD ["python", "src/main.py"]

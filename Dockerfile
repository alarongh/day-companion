FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 companion && mkdir /app/data && chown companion:companion /app/data
COPY companion ./companion
COPY scripts ./scripts
COPY examples ./examples
USER companion
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 CMD ["python","scripts/healthcheck.py"]
CMD ["python","-m","companion.app"]

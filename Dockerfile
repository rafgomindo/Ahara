FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV AHARA_TRANSPORT=streamable-http \
    AHARA_HOST=0.0.0.0 \
    AHARA_PORT=8000

EXPOSE 8000

CMD ["python", "server.py"]

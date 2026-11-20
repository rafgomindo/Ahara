FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# The robot-check page needs a fixed port so it can be published (see docker-compose.yml).
ENV AHARA_TRANSPORT=streamable-http \
    AHARA_HOST=0.0.0.0 \
    AHARA_PORT=8000 \
    AHARA_RELAY_HOST=0.0.0.0 \
    AHARA_RELAY_PORT=8765

EXPOSE 8000 8765

CMD ["python", "server.py"]

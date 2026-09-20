FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY release_radar/ ./release_radar/
COPY config.json ./
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "release_radar"]

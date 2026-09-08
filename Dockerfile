FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FFM_DATA_DIR=/app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ffm ./ffm

# Persistent data (SQLite + player cache) lives in /app/data: mount a volume there
# (docker-compose does; on Railway attach a volume at /app/data in the dashboard).
CMD ["python", "-m", "ffm", "run"]

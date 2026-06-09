FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
ARG GAR_ACCESS_TOKEN
RUN if [ -n "${GAR_ACCESS_TOKEN}" ]; then \
      UV_EXTRA_INDEX_URL="https://oauth2accesstoken:${GAR_ACCESS_TOKEN}@us-central1-python.pkg.dev/declip-v2-dev/declip-python/simple/" \
      uv pip install --system .; \
    else \
      uv pip install --system .; \
    fi

COPY app ./app
COPY config ./config

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

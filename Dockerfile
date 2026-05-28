FROM python:3.11-slim

WORKDIR /app

# Dependencias de sistema mínimas
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Código
COPY *.py ./

# Directorio para datos persistentes (Render disk se monta acá)
RUN mkdir -p /var/data/myscrubs
ENV MYSCRUBS_DATA_DIR=/var/data/myscrubs
ENV ML_TOKENS_PATH=/var/data/myscrubs/tokens.json
ENV PYTHONUNBUFFERED=1

# Puerto (Render lo inyecta vía env PORT)
EXPOSE 8000

# Default: arranca HTTP server. El cron usa COMMAND override para correr el agente.
CMD ["python", "serve_http.py"]

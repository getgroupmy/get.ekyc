# Multi-arch: builds natively on Apple Silicon (arm64) and Intel/AMD Synology (amd64).
# Same base as get-ride's deploy/face-server, which runs on the DS224+ (Celeron J4125).
FROM python:3.11.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    EKYC_DATA_DIR=/data EKYC_MODELS_DIR=/app/models

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# fastmrz is installed without its PyMuPDF dependency (AGPL, only needed for PDF input).
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --no-deps fastmrz==3.1.1

COPY scripts/download_models.py scripts/
RUN python scripts/download_models.py /app/models

COPY app ./app

RUN useradd --uid 10001 --create-home ekyc && mkdir -p /data && chown ekyc /data
USER ekyc
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]

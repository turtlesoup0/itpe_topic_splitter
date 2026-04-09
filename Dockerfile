# ── Stage 1: kordoc 빌드 ─────────────────────────────────────────
FROM node:20-slim AS kordoc-build
WORKDIR /build
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/chrisryugj/kordoc.git .
RUN npm ci && npm run build

# ── Stage 2: 런타임 ─────────────────────────────────────────────
FROM python:3.11-slim

# Node.js + Tesseract OCR 설치
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl \
        tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# kordoc 복사
COPY --from=kordoc-build /build /app/kordoc
# node_modules도 필요
COPY --from=kordoc-build /build/node_modules /app/kordoc/node_modules

# Python 의존성
RUN pip install --no-cache-dir \
    fastapi==0.115.* \
    uvicorn[standard]==0.34.* \
    python-multipart==0.0.* \
    PyMuPDF==1.25.*

# 앱 코드 복사
COPY scripts/ /app/scripts/
COPY web/ /app/web/

# kordoc CLI 경로를 환경변수로 (split_odl.py에서 참조)
ENV KORDOC_CLI=/app/kordoc/dist/cli.js

# 포트
EXPOSE 8080

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080"]

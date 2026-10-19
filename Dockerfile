# 呷安心 — 最小可重現容器（Python 版本與本機開發一致：3.12）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /srv

COPY requirements.txt ./
RUN pip install -r requirements.txt

# runtime 需要 app/ 與 data/（Ingredient Bridge corpus、藥品別名 overlay 等檔案型資料）。
# 不複製 .env / tests / docs（見 .dockerignore）；secrets 一律由執行時環境變數注入。
COPY app ./app
COPY data ./data

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# PORT 可由平台覆寫，預設 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

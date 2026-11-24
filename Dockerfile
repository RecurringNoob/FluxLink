# FluxLink Chat — hosted, read-only demo (one URL: chat page + API).
#
#   docker build -t fluxlink .
#   docker run -p 8000:8000 -e GEMINI_API_KEY=... fluxlink     →  http://localhost:8000
#
# Serves the indexes in deploy/index (create them with
#   python -m fluxdiff.rag.repos export deploy/index
# after indexing locally), so nothing is indexed on the server.
# Hosts such as Render set PORT; the app listens on it.

# ---- 1. Chat page (React) -------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY fluxdiff-ui/fluxlink/package.json fluxdiff-ui/fluxlink/package-lock.json ./
RUN npm install --no-audit --no-fund
COPY fluxdiff-ui/fluxlink/ ./
# Same-origin API under /api; chat-only build (no board viewer link).
ENV VITE_API_URL=/api VITE_CHAT_ONLY=1
RUN npm run build

# ---- 2. Chat API (Python) -------------------------------------------------
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY fluxdiff ./fluxdiff
RUN pip install ".[rag]"
COPY --from=web /web/dist ./web
COPY deploy/index ./index

ENV FLUXLINK_FRONTEND_DIST=/app/web \
    FLUXDIFF_VECTOR_DB_PATH=/app/index \
    FLUXLINK_READONLY=1 \
    FLUXLINK_ALLOW_LOCAL_REPOS=0 \
    PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn fluxdiff.rag.server:app --host 0.0.0.0 --port ${PORT}"]

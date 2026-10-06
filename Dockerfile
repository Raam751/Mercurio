# Procurement Request Copilot - production image
#
# One process: Streamlit on $PORT. The mock vendor-risk API starts automatically
# inside the same process on 127.0.0.1:8001 (src/mock_service.py), so only one
# port is exposed.
#
# LLM: Kiro CLI is installed into the image. Pass KIRO_API_KEY as a runtime
# secret (never bake it into the image). Without it the app runs rules-only.

FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
USER app
ENV HOME=/home/app \
    PATH=/home/app/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /home/app/app

# Kiro CLI (official installer; binaries go to ~/.local/bin). Disable background auto-update in the container.
RUN curl -fsSL https://cli.kiro.dev/install | bash \
    && kiro-cli --version \
    && kiro-cli settings app.disableAutoupdates true

COPY --chown=app:app requirements.txt .
RUN python -m pip install --user -r requirements.txt

COPY --chown=app:app . .

ENV PORT=8501 \
    LLM_PROVIDER=auto \
    MODEL_NAME=claude-haiku-4.5 \
    VENDOR_RISK_BASE_URL=http://127.0.0.1:8001 \
    AUTO_START_MOCK_API=1

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fs "http://127.0.0.1:${PORT}/_stcore/health" || exit 1

CMD ["sh", "-c", "python -m streamlit run app.py --server.port ${PORT} --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false"]

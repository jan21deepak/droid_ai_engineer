FROM python:3.12-slim

# git: workspace clones / branch pushes; curl: install the Droid CLI below.
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the Droid CLI (the droid-sdk Python package drives it as a subprocess).
RUN curl -fsSL https://app.factory.ai/cli | sh \
    && droid --version

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /srv /data
USER appuser
ENV HOME=/home/appuser
ENV PATH="/home/appuser/.local/bin:${PATH}"

# Sessions and settings persist under /home/appuser/.factory so runs stay resumable.
ENV DATABASE_URL=sqlite:////data/tasks.db
ENV WORKSPACE_ROOT=/data/workspace

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8000"]

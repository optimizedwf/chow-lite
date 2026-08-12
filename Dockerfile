FROM python:3.12-slim

WORKDIR /app

# system deps for bash nodes (git, curl) — keep image small
RUN apt-get update && apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY chowlite ./chowlite
COPY schemas ./schemas

RUN pip install --no-cache-dir -e . google-cloud-firestore

# Cloud Run expects a server; expose the operator API via a small FastAPI
# entrypoint (deploy/server.py) on $PORT.
ENV PORT=8080
EXPOSE 8080
CMD ["uvicorn", "deploy.server:app", "--host", "0.0.0.0", "--port", "8080"]

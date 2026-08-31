import json
import logging
import sys
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="build-pilot")

# Log straight to stdout so Railway's deploy logs pick everything up. Railway
# (and Docker generally) captures stdout/stderr, but Python buffers stdout
# when it isn't attached to a TTY, so without an explicit unbuffered stream
# handler prints can sit in a buffer and never show up in `railway logs`.
logger = logging.getLogger("build-pilot.webhook")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.propagate = False


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, bool]:
    body_bytes = await request.body()

    try:
        payload: Any = json.loads(body_bytes) if body_bytes else None
        payload_repr = json.dumps(payload, indent=2)
    except ValueError:
        payload = None
        payload_repr = body_bytes.decode("utf-8", errors="replace")

    if isinstance(payload, dict):
        workflow_run = payload.get("workflow_run", {}) or {}
        repository = payload.get("repository", {}) or {}

        repo_name = repository.get("name")
        repo_url = repository.get("html_url")
        logs_url = workflow_run.get("logs_url")
        commit_hash = workflow_run.get("head_sha")

        logger.info(
            "Parsed fields -> repo_name=%s repo_url=%s logs_url=%s commit_hash=%s",
            repo_name, repo_url, logs_url, commit_hash,
        )

    logger.info(
        "Webhook received: %s %s\nQuery params: %s\nHeaders: %s\nBody:\n%s",
        request.method,
        request.url.path,
        dict(request.query_params),
        dict(request.headers),
        payload_repr,
    )

    return {"ok": True}

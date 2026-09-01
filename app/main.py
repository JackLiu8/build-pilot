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
    except ValueError:
        payload = None

    repo_name = repo_url = logs_url = commit_hash = None
    if isinstance(payload, dict):
        workflow_run = payload.get("workflow_run", {}) or {}
        repository = payload.get("repository", {}) or {}

        repo_name = repository.get("name")
        repo_url = repository.get("html_url")
        logs_url = workflow_run.get("logs_url")
        commit_hash = workflow_run.get("head_sha")

    # One compact line per request. We deliberately stopped dumping the full
    # JSON body here -- GitHub's workflow_run payloads are large enough that
    # printing them line-by-line hit Railway's 500 logs/sec rate limit and
    # silently dropped the middle of the payload (including this exact data)
    # before it ever reached the console.
    logger.info(
        "Webhook received: %s %s | repo_name=%s repo_url=%s logs_url=%s commit_hash=%s | body_bytes=%d",
        request.method,
        request.url.path,
        repo_name,
        repo_url,
        logs_url,
        commit_hash,
        len(body_bytes),
    )

    return {"ok": True}

import json
import logging
import os
import sys
from typing import Any

import boto3
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

# SQS wiring (Session 5). The API only ever calls SendMessage -- it never
# polls or receives. If these env vars aren't set (e.g. running locally
# without AWS configured), enqueue_failed_build() just logs and no-ops
# instead of crashing the whole request.
AWS_REGION = os.environ.get("AWS_REGION")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
_sqs = boto3.client("sqs", region_name=AWS_REGION) if AWS_REGION else None


def enqueue_failed_build(job: dict) -> str | None:
    """Put one failed-run job on the queue and return its SQS MessageId.

    Keep this fast and keep the message small (SQS caps a message body at
    256 KB, and CI logs can easily be bigger) -- just enough for the worker
    to go fetch the rest later: repo, branch, commit, and a logs_url.
    """
    if _sqs is None or not SQS_QUEUE_URL:
        logger.warning("SQS not configured (missing AWS_REGION/SQS_QUEUE_URL); skipping enqueue")
        return None

    resp = _sqs.send_message(QueueUrl=SQS_QUEUE_URL, MessageBody=json.dumps(job))
    return resp["MessageId"]


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
    message_id = None

    if isinstance(payload, dict):
        workflow_run = payload.get("workflow_run", {}) or {}
        repository = payload.get("repository", {}) or {}

        repo_name = repository.get("name")
        repo_url = repository.get("html_url")
        logs_url = workflow_run.get("logs_url")
        commit_hash = workflow_run.get("head_sha")

        # Only enqueue actual CI failures. Ping, success, and every other
        # workflow_run status/conclusion: log it and move on, nothing goes
        # on the queue. GitHub is impatient -- SendMessage, then return 200
        # immediately. All the slow work (downloading logs, calling a model)
        # happens later in the worker, off this request entirely.
        if workflow_run.get("status") == "completed" and workflow_run.get("conclusion") == "failure":
            full_name = repository.get("full_name")
            run_id = workflow_run.get("id")

            if not logs_url and full_name and run_id:
                logs_url = f"https://api.github.com/repos/{full_name}/actions/runs/{run_id}/logs"

            job = {
                "run_id": run_id,
                "repo": full_name,
                "branch": workflow_run.get("head_branch"),
                "commit": commit_hash,
                "logs_url": logs_url,
                "workflow_name": workflow_run.get("name"),
                "workflow_path": workflow_run.get("path"),
            }
            message_id = enqueue_failed_build(job)

    # One compact line per request. We deliberately stopped dumping the full
    # JSON body here -- GitHub's workflow_run payloads are large enough that
    # printing them line-by-line hit Railway's 500 logs/sec rate limit and
    # silently dropped the middle of the payload (including this exact data)
    # before it ever reached the console.
    logger.info(
        "Webhook received: %s %s | repo_name=%s repo_url=%s logs_url=%s commit_hash=%s | body_bytes=%d | sqs_message_id=%s",
        request.method,
        request.url.path,
        repo_name,
        repo_url,
        logs_url,
        commit_hash,
        len(body_bytes),
        message_id,
    )

    return {"ok": True}

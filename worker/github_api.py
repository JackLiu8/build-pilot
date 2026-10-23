"""Shared GitHub Actions log fetching + S3 archiving.

Used by both the Session 5/6 pipeline bookkeeping in worker/sqs_worker.py
and the Stage 2 agent's prepare() node (worker/agent.py), so there's one
implementation instead of two copies drifting apart.
"""
from __future__ import annotations

import logging
from io import BytesIO
from zipfile import ZipFile

import requests

logger = logging.getLogger("build-pilot.github_api")


def fetch_log_text(logs_url: str, github_token: str) -> str:
    """Download logs_url (it 302-redirects to a temporary signed .zip) and
    return the concatenated plain-text contents of every step log inside it.
    """
    response = requests.get(
        logs_url,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60,
    )
    response.raise_for_status()

    parts = []
    with ZipFile(BytesIO(response.content)) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith(".txt"):
                parts.append(archive.read(name).decode("utf-8", errors="replace"))
    return "\n".join(parts)


def upload_log_text_to_s3(s3_client, bucket: str, run_id, commit_sha: str, text: str) -> str:
    """Upload extracted log text to S3 under the same runs/{run_id}/{commit}.log
    key the Session 6 pipeline used, and return the s3:// URI.
    """
    key = f"runs/{run_id}/{commit_sha}.log"
    s3_client.put_object(Bucket=bucket, Key=key, Body=text.encode(), ContentType="text/plain")
    uri = f"s3://{bucket}/{key}"
    logger.info("Uploaded logs for run_id=%s -> %s", run_id, uri)
    return uri

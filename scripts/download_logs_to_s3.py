"""Session 6 homework: pull one failed-build job off the SQS queue, download
its raw GitHub Actions logs, and upload them into S3.

This is a standalone script (not wired into the continuous worker yet) --
run it manually with:

    python scripts/download_logs_to_s3.py

It reuses the same env vars as worker/sqs_worker.py (AWS_REGION,
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, SQS_QUEUE_URL) plus two new ones:

    GITHUB_TOKEN   a personal access token with Actions: Read-only,
                   needed because logs_url is an authenticated GitHub API
                   endpoint even for a public/your-own repo
    S3_BUCKET      the bucket created for this homework, e.g.
                   build-pilot-logs-<account-id>

Note on the download: logs_url doesn't return plain text. It 302-redirects
to a temporary signed URL for a .zip archive containing one .txt file per
job step. requests follows the redirect automatically, so what lands in
`data` below is zip bytes -- that's why this uploads a `logs.zip`, not a
`.txt`/`.log` file.
"""
import json
import logging
import os
import sys
from typing import Optional

import boto3
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger("build-pilot.download-logs")

AWS_REGION = os.environ["AWS_REGION"]
SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
S3_BUCKET = os.environ["S3_BUCKET"]

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)


def fetch_one_job() -> Optional[dict]:
    """Peek one failed-build job off the queue without deleting it, so this
    script can be re-run without stealing messages the real worker (from
    last session) would otherwise still need to process.
    """
    resp = sqs.receive_message(QueueUrl=SQS_QUEUE_URL, MaxNumberOfMessages=1, WaitTimeSeconds=5)
    messages = resp.get("Messages", [])
    if not messages:
        return None
    return json.loads(messages[0]["Body"])


def download_logs(logs_url: str) -> bytes:
    resp = requests.get(
        logs_url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def upload_to_s3(repo: str, run_id: int, data: bytes) -> str:
    key = f"{repo}/{run_id}/logs.zip"
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType="application/zip")
    return key


def main() -> None:
    job = fetch_one_job()
    if job is None:
        logger.info("No messages on the queue right now. Trigger a failing CI run first, then re-run this.")
        return

    logger.info("Got job: %s", job)

    data = download_logs(job["logs_url"])
    logger.info("Downloaded %d bytes of logs", len(data))

    key = upload_to_s3(job["repo"], job["run_id"], data)
    logger.info("Uploaded to s3://%s/%s", S3_BUCKET, key)


if __name__ == "__main__":
    main()

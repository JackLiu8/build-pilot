"""Session 5 + 6 worker: long-poll the SQS queue, and for each failed-build
job, do the full pipeline the homework asks for:

    1. persist it to Postgres -- upsert a row in `repositories`, insert a
       row in `workflow_runs` (Session 6)
    2. download the run's GitHub Actions logs and extract the plain-text
       content out of the .zip GitHub hands back (Session 6)
    3. upload that text to S3 and record the resulting s3:// URI back onto
       the workflow_runs row (Session 6)

Run with:

    python -m worker.sqs_worker

Config comes from the environment: AWS_REGION, AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, SQS_QUEUE_URL (Session 5) plus, as of Session 6:
GITHUB_TOKEN, S3_BUCKET, DATABASE_URL. A local .env file (gitignored) holds
these when running by hand -- see .env.example.
"""

import json
import logging
import os
import sys
from io import BytesIO
from zipfile import ZipFile

import boto3
import requests

from worker import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build-pilot.worker")

AWS_REGION = os.environ["AWS_REGION"]
SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
S3_BUCKET = os.environ["S3_BUCKET"]

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)


def fetch_log_text(logs_url: str) -> str:
    """Download logs_url (it 302-redirects to a temporary signed .zip) and
    return the concatenated plain-text contents of every step log inside it.
    GitHub packs one .txt file per job step into the archive; step
    boundaries don't matter here, so this just joins them in order.
    """
    response = requests.get(
        logs_url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60,
    )  # requests follows GitHub's redirect by default
    response.raise_for_status()

    parts = []
    with ZipFile(BytesIO(response.content)) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith(".txt"):
                parts.append(archive.read(name).decode("utf-8", errors="replace"))
    return "\n".join(parts)


def process_job(job: dict) -> None:
    """Run the full pipeline for one failed-build job: DB rows, then logs."""
    repo_id = db.upsert_repository(job["repo"])
    db.insert_workflow_run(repo_id, job)

    text = fetch_log_text(job["logs_url"])
    key = f"runs/{job['run_id']}/{job['commit']}.log"
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=text.encode(), ContentType="text/plain")
    uri = f"s3://{S3_BUCKET}/{key}"

    db.set_logs_s3_uri(job["run_id"], uri)
    logger.info("Processed run_id=%s -> %s", job["run_id"], uri)


def main() -> None:
    db.init_db()
    logger.info("Worker starting. Long-polling queue: %s", SQS_QUEUE_URL)

    while True:
        resp = sqs.receive_message(
            QueueUrl=SQS_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,  # long poll: fewer empty responses than short polling
            VisibilityTimeout=60,  # job is hidden (not removed) while we "work" on it
        )

        for msg in resp.get("Messages", []):
            job = json.loads(msg["Body"])
            logger.info("job %s", job)

            try:
                process_job(job)
            except Exception:
                # Do NOT delete on failure. Leave it on the queue -- after
                # VisibilityTimeout expires, SQS hands it to a worker again.
                logger.exception("Failed to process run_id=%s; leaving on queue for retry", job.get("run_id"))
                continue

            sqs.delete_message(
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=msg["ReceiptHandle"],
            )
            logger.info("Deleted message for run_id=%s (done)", job.get("run_id"))


if __name__ == "__main__":
    main()

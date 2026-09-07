"""Session 5 worker: long-poll the SQS queue and print each failed-build job.

Run with:

    python -m worker.sqs_worker

Config comes from the environment: AWS_REGION, AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, SQS_QUEUE_URL (a local .env file, not committed).
This process only ever calls ReceiveMessage / DeleteMessage -- it never
calls SendMessage, that's the API's job (see app/main.py).
"""

import json
import logging
import os
import sys

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build-pilot.worker")

AWS_REGION = os.environ["AWS_REGION"]
SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]

sqs = boto3.client("sqs", region_name=AWS_REGION)


def main() -> None:
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
                # Session 5 scope is just proving delivery end to end: receive,
                # print, delete. Actually downloading logs_url and doing
                # anything with it is later-session work.
                pass
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

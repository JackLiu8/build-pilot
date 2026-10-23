"""Session 5 + 6 + Stage 2 (Session 11) worker.

Long-polls the SQS queue. For each failed-build job:

    1. persist it to Postgres -- upsert a row in `repositories`, insert a
       row in `workflow_runs` (Session 6)
    2. run the Stage 2 remediation loop (worker/agent.py): prepare() clones
       the failed commit and archives the raw logs to S3, remediate() asks
       Claude Code to diagnose + edit the sandbox, validate() discovers and
       runs a local test gate. Fail -> retry remediate(). Pass or skipped
       gate -> worker opens one draft PR (worker/git_pr.py). Exhausted
       retries -> logged as needs_manual, no PR, no silent merge.

Run with:

    python -m worker.sqs_worker

Config comes from the environment: AWS_REGION, AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, SQS_QUEUE_URL (Session 5) plus GITHUB_TOKEN,
S3_BUCKET, DATABASE_URL (Session 6) and, as of Session 11,
AGENT_MAX_ATTEMPTS (default 3). A local .env file (gitignored) holds these
when running by hand -- see .env.example.
"""

import json
import logging
import os
import shutil
import sys

import boto3

from worker import db
from worker.agent import AgentState, build_graph
from worker.git_pr import create_draft_pr_if_needed

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
AGENT_MAX_ATTEMPTS = int(os.environ.get("AGENT_MAX_ATTEMPTS", "3"))

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)

_agent_graph = None


def _get_agent_graph():
    """Build (and cache) the compiled LangGraph once per process, not once
    per job."""
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = build_graph()
    return _agent_graph


def run_remediation(job: dict) -> None:
    """Run prepare -> remediate -> validate (with retries) for one job, then
    open a draft PR if it passed (or the gate was skipped). Always cleans
    up the sandbox, win or lose.
    """
    initial_state: AgentState = {
        "repo_url": f"https://github.com/{job['repo']}.git",
        "repo_full_name": job["repo"],
        "commit": job["commit"],
        "logs_url": job["logs_url"],
        "github_run_id": job["run_id"],
        "github_token": GITHUB_TOKEN,
        "s3_client": s3,
        "s3_bucket": S3_BUCKET,
        "max_attempts": AGENT_MAX_ATTEMPTS,
    }

    graph = _get_agent_graph()
    final_state = graph.invoke(initial_state)

    if final_state.get("logs_s3_uri"):
        db.set_logs_s3_uri(job["run_id"], final_state["logs_s3_uri"])

    sandbox = final_state.get("sandbox")
    try:
        if final_state.get("passed"):
            gate_skipped = "no local gate" in final_state.get("test_output", "")
            pr_url = create_draft_pr_if_needed(
                sandbox=sandbox,
                repo_full_name=job["repo"],
                github_run_id=job["run_id"],
                diagnosis=final_state.get("diagnosis", ""),
                test_output=final_state.get("test_output", ""),
                gate_skipped=gate_skipped,
                github_token=GITHUB_TOKEN,
            )
            logger.info("Remediation validated for run_id=%s -> PR: %s", job["run_id"], pr_url)
        else:
            logger.warning(
                "Remediation needs_manual for run_id=%s after %s attempt(s); no PR opened. test_output=%s",
                job["run_id"], final_state.get("attempt"), final_state.get("test_output", "")[:500],
            )
    finally:
        # Never leave cloned repos on disk -- win, lose, or crash.
        if sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)


def process_job(job: dict) -> None:
    """Persist the failure, then run the Stage 2 remediation loop (which
    itself archives the CI logs to S3 as part of prepare())."""
    repo_id = db.upsert_repository(job["repo"])
    db.insert_workflow_run(repo_id, job)

    run_remediation(job)
    logger.info("Finished run_id=%s", job["run_id"])


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

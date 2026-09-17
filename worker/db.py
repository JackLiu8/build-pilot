"""Session 6: persistence layer for the worker.

Every failed-build job the worker pulls off SQS gets written to Postgres
(the RDS database from this session's homework) as it's processed -- one row
in `repositories` per repo (upserted, so re-runs don't duplicate it), one row
in `workflow_runs` per failed run, with `logs_s3_uri` filled in once the logs
have actually been uploaded to S3.

Plain psycopg2, no ORM -- two tables is not enough surface area to justify
pulling in SQLAlchemy.
"""
import logging
import os

import psycopg2

logger = logging.getLogger("build-pilot.db")

DATABASE_URL = os.environ["DATABASE_URL"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id SERIAL PRIMARY KEY,
    full_name TEXT UNIQUE NOT NULL,
    html_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id SERIAL PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    github_run_id BIGINT UNIQUE NOT NULL,
    branch TEXT,
    commit_sha TEXT,
    workflow_name TEXT,
    workflow_path TEXT,
    logs_s3_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    """Create the repositories/workflow_runs tables if they don't exist yet.

    Safe to call every time the worker starts up -- CREATE TABLE IF NOT
    EXISTS is a no-op once the schema is already in place, so there's no
    separate migration step to remember to run.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
    logger.info("Database schema ready (repositories, workflow_runs)")


def upsert_repository(full_name: str) -> int:
    """Insert the repo if we haven't seen it before; return its id either way."""
    html_url = f"https://github.com/{full_name}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO repositories (full_name, html_url)
                VALUES (%s, %s)
                ON CONFLICT (full_name) DO UPDATE SET full_name = EXCLUDED.full_name
                RETURNING id
                """,
                (full_name, html_url),
            )
            repo_id = cur.fetchone()[0]
        conn.commit()
    return repo_id


def insert_workflow_run(repository_id: int, job: dict) -> int:
    """Insert one failed-run row. If this run_id has already been recorded
    (e.g. the worker gets restarted and re-processes a message), update the
    existing row instead of erroring on the unique constraint.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_runs
                    (repository_id, github_run_id, branch, commit_sha, workflow_name, workflow_path)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (github_run_id) DO UPDATE SET
                    branch = EXCLUDED.branch,
                    commit_sha = EXCLUDED.commit_sha,
                    workflow_name = EXCLUDED.workflow_name,
                    workflow_path = EXCLUDED.workflow_path
                RETURNING id
                """,
                (
                    repository_id,
                    job["run_id"],
                    job.get("branch"),
                    job.get("commit"),
                    job.get("workflow_name"),
                    job.get("workflow_path"),
                ),
            )
            run_row_id = cur.fetchone()[0]
        conn.commit()
    return run_row_id


def set_logs_s3_uri(github_run_id: int, uri: str) -> None:
    """Record where this run's extracted log text landed in S3."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET logs_s3_uri = %s WHERE github_run_id = %s",
                (uri, github_run_id),
            )
        conn.commit()
    logger.info("Set logs_s3_uri for run_id=%s -> %s", github_run_id, uri)

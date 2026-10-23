"""Worker-owned Git + GitHub operations: branch, commit, push, draft PR.

Not a graph node -- LangGraph decides pass/fail, the worker owns Git, same
as it already owns SQS ack/delete. Idempotent by construction: redelivery
of the same github_run_id reuses the same branch name and checks for an
existing open PR before opening a second one.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import requests

logger = logging.getLogger("build-pilot.git_pr")

GITHUB_API = "https://api.github.com"


def _branch_name(github_run_id) -> str:
    return f"build-pilot/run-{github_run_id}"


def _find_existing_pr(repo_full_name: str, branch: str, github_token: str) -> str | None:
    owner = repo_full_name.split("/")[0]
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo_full_name}/pulls",
        headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"},
        params={"head": f"{owner}:{branch}", "state": "open"},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0]["html_url"] if results else None


def create_draft_pr_if_needed(
    *,
    sandbox: str,
    repo_full_name: str,
    github_run_id,
    diagnosis: str,
    test_output: str,
    gate_skipped: bool,
    github_token: str,
) -> str | None:
    """Commit whatever remediate() changed in the sandbox, push a branch
    named build-pilot/run-{github_run_id}, and open one draft PR. If a PR
    for this branch is already open (SQS redelivered the same job), return
    its URL instead of opening a duplicate.
    """
    branch = _branch_name(github_run_id)

    existing = _find_existing_pr(repo_full_name, branch, github_token)
    if existing:
        logger.info("PR already open for run_id=%s: %s", github_run_id, existing)
        return existing

    sandbox_path = Path(sandbox)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=sandbox_path, capture_output=True, text=True, check=True,
    )
    if not status.stdout.strip():
        logger.info("Nothing for Claude to commit for run_id=%s; skipping PR", github_run_id)
        return None

    subprocess.run(["git", "checkout", "-b", branch], cwd=sandbox_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=sandbox_path, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git", "-c", "user.email=build-pilot@users.noreply.github.com", "-c", "user.name=build-pilot",
            "commit", "-m", f"build-pilot: automated fix attempt for failed run {github_run_id}",
        ],
        cwd=sandbox_path, check=True, capture_output=True, text=True,
    )

    push_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    subprocess.run(
        ["git", "push", push_url, f"HEAD:{branch}"],
        cwd=sandbox_path, check=True, capture_output=True, text=True,
    )

    body_lines = [
        f"Automated remediation attempt for failed run `{github_run_id}`.",
        "",
        "**Diagnosis (from Claude Code):**",
        diagnosis or "(no diagnosis captured)",
        "",
    ]
    if gate_skipped:
        body_lines += ["**Test gate:** skipped -- no Makefile/package.json/README test command found in this repo."]
    else:
        body_lines += ["**Test gate output (tail):**", "```", test_output[-1500:], "```"]
    body_lines += ["", "_Opened automatically by build-pilot. Review before merging._"]

    resp = requests.post(
        f"{GITHUB_API}/repos/{repo_full_name}/pulls",
        headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"},
        json={
            "title": f"build-pilot: automated fix for failed run {github_run_id}",
            "head": branch,
            "base": "main",
            "body": "\n".join(body_lines),
            "draft": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    pr_url = resp.json()["html_url"]
    logger.info("Opened draft PR for run_id=%s: %s", github_run_id, pr_url)
    return pr_url

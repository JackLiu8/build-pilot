"""Stage 2 (Session 11): the LangGraph remediation loop.

START -> prepare -> remediate -> validate -> (retry remediate | done | needs_manual)

- prepare(): deterministic, no model. New temp sandbox, git clone the failed
  SHA, write the CI failure log into that folder, upload a durable copy to
  S3. Runs once per job; retries go back to remediate(), not prepare().
- remediate(): the one Claude Code node. `claude -p` with cwd=sandbox can
  Read the repo + ci-failure.log and Edit files directly -- diagnose +
  retrieve + patch collapsed into one call. No {path, old, new} contract.
  Must not run tests, commit, or push.
- validate(): no model grading. The one claude_json() call inside
  discover_gate_with_ai() only *proposes* a command; validate() checks the
  proposal isn't invented, runs it, and the exit code decides passed.
  Routing only needs passed + test_output -- the command itself never
  touches AgentState or Postgres.

The worker (worker/sqs_worker.py + worker/git_pr.py), not a graph node, owns
sandbox cleanup and owns creating the draft PR once validate() reports
passed=True (including a skipped gate).
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict

from worker.claude_cli import claude_json, run_claude_edit
from worker.github_api import fetch_log_text, upload_log_text_to_s3

logger = logging.getLogger("build-pilot.agent")


class AgentState(TypedDict, total=False):
    repo_url: str
    repo_full_name: str
    commit: str
    logs_url: str
    github_run_id: int
    github_token: str
    s3_client: object
    s3_bucket: str

    log_text: str
    sandbox: str
    logs_s3_uri: str

    diagnosis: str
    test_output: str
    passed: bool
    attempt: int
    max_attempts: int
    error: str


def prepare(state: AgentState) -> dict:
    """Deterministic setup: clone the failed commit, drop the CI log into
    the sandbox, archive a durable copy to S3. No Claude call in this node.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="build-pilot-"))

    subprocess.run(
        ["git", "clone", "--depth", "1", state["repo_url"], str(sandbox)],
        check=True, timeout=120, capture_output=True, text=True,
    )
    try:
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", state["commit"]],
            cwd=sandbox, check=True, timeout=60, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "checkout", state["commit"]],
            cwd=sandbox, check=True, timeout=30, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        # Shallow clone may already sit on the right commit (default branch
        # tip), or the SHA may be unreachable at depth 1. Don't fail
        # prepare() over it -- remediate() still has the log text even if
        # checkout couldn't pin the exact commit.
        logger.warning("Could not checkout commit %s exactly; continuing on clone HEAD", state["commit"])

    log_text = fetch_log_text(state["logs_url"], state["github_token"])
    (sandbox / "ci-failure.log").write_text(log_text, encoding="utf-8")

    logs_s3_uri = upload_log_text_to_s3(
        state["s3_client"], state["s3_bucket"], state["github_run_id"], state["commit"], log_text,
    )

    return {
        "sandbox": str(sandbox),
        "log_text": log_text,
        "logs_s3_uri": logs_s3_uri,
        "attempt": 0,
        "error": "",
    }


def remediate(state: AgentState) -> dict:
    """The one Claude Code node: diagnose the failure and edit the repo.
    Collapses Class 9's diagnose/retrieve/patch into a single call because
    Claude Code can read and write the sandbox directly.
    """
    prompt = (
        "You are diagnosing a failed CI build in the repository checked out "
        "in this directory. Read ci-failure.log and any relevant source "
        "files, determine the root cause, and edit the repository to fix "
        "it. Do not run tests. Do not commit or push. Keep edits minimal "
        "and scoped to the actual failure.\n\n"
        + json.dumps(
            {
                "log_text": state.get("log_text", "")[-8000:],
                "previous_test_output": state.get("test_output", "")[-4000:],
                "attempt": state.get("attempt", 0) + 1,
            }
        )
    )

    try:
        output = run_claude_edit(prompt, cwd=state["sandbox"], timeout=300)
        diagnosis = output[-2000:]
        error = ""
    except Exception as exc:  # noqa: BLE001 - surface CLI failure as node error, don't crash the worker
        logger.exception("remediate() failed on attempt %s", state.get("attempt", 0) + 1)
        diagnosis = state.get("diagnosis", "")
        error = str(exc)

    return {
        "attempt": state.get("attempt", 0) + 1,
        "diagnosis": diagnosis,
        "passed": False,
        "error": error,
    }


def discover_gate_with_ai(sandbox: Path) -> dict:
    """Ask Claude (via claude_json()) to propose a post-patch verification
    command: Makefile first, then package.json, then README/CONTRIBUTING.
    Returns {"command": [...] | None, "source": str | None, "detail": str}.
    Claude proposes; validate() checks the proposal is honest and runs it.
    """
    makefile = sandbox / "Makefile"
    package_json = sandbox / "package.json"
    readme = next(
        (p for p in (sandbox / "README.md", sandbox / "README", sandbox / "CONTRIBUTING.md") if p.exists()),
        None,
    )

    bounded_context = {
        "makefile": makefile.read_text(encoding="utf-8", errors="replace")[:3000] if makefile.exists() else None,
        "package_json": package_json.read_text(encoding="utf-8", errors="replace")[:3000] if package_json.exists() else None,
        "readme_excerpt": readme.read_text(encoding="utf-8", errors="replace")[:3000] if readme else None,
    }

    if not any(bounded_context.values()):
        return {"command": None, "source": None, "detail": "no Makefile, package.json, or README found"}

    system_prompt = (
        "You find the right command to verify a code change in a repository. "
        "Prefer a Makefile target named test or check. If there is no "
        "Makefile, use a package.json \"test\" script. If neither exists, "
        "read the README/CONTRIBUTING excerpt and use whatever the project "
        "documents. Never invent a command (e.g. pytest) the repo never "
        "mentions, and never propose setup/install commands. If nothing "
        "defensible is documented, return command: null."
    )
    user_prompt = (
        "Repository context (possibly truncated):\n" + json.dumps(bounded_context)
        + "\n\nRespond with JSON: {\"command\": [\"make\", \"test\"] or null, "
          "\"source\": \"Makefile\" | \"package.json\" | \"README\" | null, "
          "\"detail\": \"short justification, e.g. 'target test'\"}"
    )

    try:
        proposal = claude_json(system_prompt, user_prompt, cwd=str(sandbox), timeout=120)
    except Exception:
        logger.exception("discover_gate_with_ai() failed; treating as no defensible gate")
        return {"command": None, "source": None, "detail": "gate discovery failed"}

    return proposal


def validate(state: AgentState) -> dict:
    """No model grading here. Discover a command, confirm it isn't invented,
    run it, and let the exit code decide passed.
    """
    sandbox = Path(state["sandbox"])
    proposal = discover_gate_with_ai(sandbox)
    command = proposal.get("command")

    if not command:
        return {"passed": True, "test_output": "no local gate found; skipping tests and opening PR"}

    if proposal.get("source") == "Makefile":
        target = command[-1] if command[:1] == ["make"] else None
        makefile_text = (
            (sandbox / "Makefile").read_text(encoding="utf-8", errors="replace")
            if (sandbox / "Makefile").exists() else ""
        )
        if not target or f"{target}:" not in makefile_text or command != ["make", target]:
            raise ValueError(f"invented Makefile target: {proposal!r}")

    completed = subprocess.run(
        command, cwd=sandbox, capture_output=True, text=True, timeout=300, shell=False,
    )
    return {
        "passed": completed.returncode == 0,
        "test_output": (completed.stdout + completed.stderr)[-4000:],
    }


def decide(state: AgentState) -> str:
    """Routing: validated/skipped gate -> done, exhausted retries ->
    needs_manual, otherwise -> retry (back to remediate(), not prepare()).
    """
    if state.get("passed"):
        return "done"
    if state.get("attempt", 0) >= state.get("max_attempts", 3):
        return "needs_manual"
    return "retry"


def build_graph():
    """Compile the LangGraph: START -> prepare -> remediate -> validate,
    with validate routing back to remediate on failure (never back to
    prepare -- the sandbox already exists).
    """
    from langgraph.graph import END, StateGraph

    graph = StateGraph(AgentState)
    graph.add_node("prepare", prepare)
    graph.add_node("remediate", remediate)
    graph.add_node("validate", validate)

    graph.set_entry_point("prepare")
    graph.add_edge("prepare", "remediate")
    graph.add_edge("remediate", "validate")
    graph.add_conditional_edges(
        "validate",
        decide,
        {"retry": "remediate", "done": END, "needs_manual": END},
    )

    return graph.compile()

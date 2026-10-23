"""Stage 2: thin helpers around the Claude Code CLI.

Two different ways of calling into Claude Code live here:

- claude_json() is the "give me structured JSON back" helper -- the same
  (system, user) -> dict shape Class 9's chat_json() had, just backed by
  `claude -p` instead of litellm.completion. Used for gate discovery.

- run_claude_edit() is the "let Claude actually read/edit files in a
  sandbox" helper used by remediate(). It runs `claude -p <prompt>` with
  cwd set to the sandbox so Claude's file tools operate on the cloned repo,
  and just returns whatever Claude printed -- remediate() doesn't need
  structured JSON back, just the diagnosis text for logging/the PR body.

Pick one helper per call site. Neither touches LiteLLM.
"""
from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger("build-pilot.claude_cli")


class ClaudeCLIError(RuntimeError):
    """Raised when the claude CLI exits non-zero or produces no usable output."""


def _run_claude(prompt: str, *, cwd: str | None = None, timeout: int = 120) -> str:
    completed = subprocess.run(
        ["claude", "-p", prompt],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise ClaudeCLIError(f"claude -p exited {completed.returncode}: {completed.stderr[-2000:]}")
    return completed.stdout


def _extract_json(text: str) -> dict:
    """Claude sometimes wraps JSON in a ```json fence or adds prose around
    it. Pull out the first {...} block rather than assuming stdout is
    clean JSON end to end.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ClaudeCLIError(f"no JSON object found in claude output: {text[:500]!r}")
    return json.loads(text[start : end + 1])


def claude_json(system_prompt: str, user_prompt: str, *, cwd: str | None = None, timeout: int = 120) -> dict:
    """Ask Claude Code for one JSON object back. Tests fake this function;
    they never shell out to a real CLI.
    """
    prompt = f"{system_prompt}\n\n{user_prompt}\n\nRespond with ONLY a single JSON object, no prose, no markdown fence."
    stdout = _run_claude(prompt, cwd=cwd, timeout=timeout)
    return _extract_json(stdout)


def run_claude_edit(prompt: str, *, cwd: str, timeout: int = 300) -> str:
    """Let Claude Code read/edit files in `cwd` (the sandbox) and return
    whatever it printed. Used by remediate().
    """
    return _run_claude(prompt, cwd=cwd, timeout=timeout)

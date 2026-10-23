"""Tests for worker/claude_cli.py -- JSON extraction from `claude -p`
output. Fakes subprocess.run; never shells out to a real CLI.
"""
import subprocess

from worker import claude_cli


def test_claude_json_parses_fenced_json(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 0, stdout='```json\n{"command": null, "source": null, "detail": "none"}\n```', stderr="",
        ),
    )

    result = claude_cli.claude_json("sys", "user")

    assert result == {"command": None, "source": None, "detail": "none"}


def test_claude_json_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="boom"),
    )

    try:
        claude_cli.claude_json("sys", "user")
        assert False, "expected ClaudeCLIError"
    except claude_cli.ClaudeCLIError:
        pass

"""Tests for the Stage 2 remediation loop (worker/agent.py). No real AWS,
GitHub, or live Claude CLI -- claude_json() and subprocess are faked.
"""
import subprocess

import pytest

from worker import agent


def _make_sandbox(tmp_path, makefile_body=None):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    if makefile_body is not None:
        (sandbox / "Makefile").write_text(makefile_body)
    return sandbox


def test_validate_accepts_approved_makefile_target(tmp_path, monkeypatch):
    sandbox = _make_sandbox(tmp_path, makefile_body="test:\n\tpytest\n")

    monkeypatch.setattr(
        agent, "claude_json",
        lambda *a, **kw: {"command": ["make", "test"], "source": "Makefile", "detail": "target test"},
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, stdout="ok", stderr=""),
    )

    result = agent.validate({"sandbox": str(sandbox)})

    assert result["passed"] is True


def test_validate_rejects_invented_makefile_target(tmp_path, monkeypatch):
    sandbox = _make_sandbox(tmp_path, makefile_body="build:\n\techo hi\n")

    monkeypatch.setattr(
        agent, "claude_json",
        lambda *a, **kw: {"command": ["make", "test"], "source": "Makefile", "detail": "target test"},
    )

    with pytest.raises(ValueError):
        agent.validate({"sandbox": str(sandbox)})


def test_validate_skips_and_still_passes_with_no_evidence(tmp_path, monkeypatch):
    sandbox = _make_sandbox(tmp_path)  # no Makefile, no package.json, no README

    monkeypatch.setattr(
        agent, "claude_json",
        lambda *a, **kw: {"command": None, "source": None, "detail": "nothing documented"},
    )

    result = agent.validate({"sandbox": str(sandbox)})

    assert result["passed"] is True
    assert "no local gate" in result["test_output"]


def test_decide_retries_then_succeeds():
    assert agent.decide({"passed": False, "attempt": 1, "max_attempts": 3}) == "retry"
    assert agent.decide({"passed": True, "attempt": 2, "max_attempts": 3}) == "done"


def test_decide_escalates_after_max_attempts():
    assert agent.decide({"passed": False, "attempt": 3, "max_attempts": 3}) == "needs_manual"


def test_fail_then_pass_retry_loop(tmp_path, monkeypatch):
    """validate() fails once (nonzero exit), decide() says retry, validate()
    passes on the second attempt, decide() says done."""
    sandbox = _make_sandbox(tmp_path, makefile_body="test:\n\tpytest\n")

    monkeypatch.setattr(
        agent, "claude_json",
        lambda *a, **kw: {"command": ["make", "test"], "source": "Makefile", "detail": "target test"},
    )

    outcomes = iter([1, 0])  # first run fails, second run passes

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, next(outcomes), stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    first = agent.validate({"sandbox": str(sandbox)})
    assert first["passed"] is False
    assert agent.decide({"passed": first["passed"], "attempt": 1, "max_attempts": 3}) == "retry"

    second = agent.validate({"sandbox": str(sandbox)})
    assert second["passed"] is True
    assert agent.decide({"passed": second["passed"], "attempt": 2, "max_attempts": 3}) == "done"

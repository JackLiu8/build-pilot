"""Tests for worker/git_pr.py -- idempotent branch/PR creation. No real
GitHub calls; requests.get/post and subprocess.run are faked.
"""
import subprocess

from worker import git_pr


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def test_returns_existing_pr_without_creating_a_second_one(monkeypatch):
    monkeypatch.setattr(
        git_pr.requests, "get",
        lambda *a, **kw: _FakeResponse([{"html_url": "https://github.com/acme/repo/pull/42"}]),
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("should not open a second PR when one is already open")

    monkeypatch.setattr(git_pr.requests, "post", fail_if_called)

    url = git_pr.create_draft_pr_if_needed(
        sandbox="/tmp/unused", repo_full_name="acme/repo", github_run_id=123,
        diagnosis="d", test_output="t", gate_skipped=False, github_token="tok",
    )

    assert url == "https://github.com/acme/repo/pull/42"


def test_opens_draft_pr_when_none_exists(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    monkeypatch.setattr(git_pr.requests, "get", lambda *a, **kw: _FakeResponse([]))
    monkeypatch.setattr(
        git_pr.requests, "post",
        lambda *a, **kw: _FakeResponse({"html_url": "https://github.com/acme/repo/pull/43"}),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, stdout=" M file.py\n", stderr=""),
    )

    url = git_pr.create_draft_pr_if_needed(
        sandbox=str(sandbox), repo_full_name="acme/repo", github_run_id=456,
        diagnosis="d", test_output="t", gate_skipped=False, github_token="tok",
    )

    assert url == "https://github.com/acme/repo/pull/43"


def test_skips_pr_when_nothing_changed(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    monkeypatch.setattr(git_pr.requests, "get", lambda *a, **kw: _FakeResponse([]))
    monkeypatch.setattr(
        subprocess, "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("should not open a PR when there is nothing to commit")

    monkeypatch.setattr(git_pr.requests, "post", fail_if_called)

    url = git_pr.create_draft_pr_if_needed(
        sandbox=str(sandbox), repo_full_name="acme/repo", github_run_id=789,
        diagnosis="d", test_output="t", gate_skipped=False, github_token="tok",
    )

    assert url is None

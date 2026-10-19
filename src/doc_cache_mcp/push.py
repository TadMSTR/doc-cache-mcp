"""The docs-cache config write path: every call finishes or fails, and says which.

``doc-sync.yml`` lives in a shared git repo that a cron and humans also use. A config change
from this tool is one atomic sequence, run under a process-wide lock by the server:

1. **Precondition, before anything is written** (:func:`precondition`). The config file has
   no uncommitted changes, and — when pushing — the checkout is on the target branch and is
   exactly level with ``<remote>/<branch>`` after a fetch. Anything else is ``refused``,
   naming the condition ("local main is 2 behind origin/main; ..."). Nothing is written.
2. **Write, then commit as the tool identity** (:func:`commit_change`). Commits are authored
   as ``doc-cache-mcp`` rather than as the host user, passed per invocation with ``-c`` so
   nobody else's identity in the same checkout changes. The commit is path-limited to the
   config file, so nothing else staged in the checkout is swept into it.
3. **Classify the change** — ``additive``, ``repoint`` or ``remove`` — from git's own view
   of the commit, and roll back if it is not the kind the calling tool performs. Adding
   cannot repoint; repointing cannot remove. The classification is recorded in the result
   and in the audit event.
4. **Guards, then push the validated SHA.** Every commit in the push range must be the
   tool's (no foreign commits ride along), and the range may touch the config file and
   nothing else. The push sends the exact SHA the guards inspected, never the ``HEAD`` ref:
   git re-resolves ``HEAD`` at push time, so anything committed in between would otherwise
   ride along unchecked.
5. **If the push fails, roll back this call's own commit**, and only that: a compare-and-swap
   of the branch ref from this commit back to its parent (``git update-ref HEAD <pre>
   <sha>``), then a restore of the config file — only if nobody has edited it since. A
   concurrent commit makes the swap fail, and then nothing is reset: the result is
   ``stranded`` and says exactly what is left where. The next call's precondition then
   refuses, naming it, so a stranded commit cannot quietly poison later calls.

The ``outcome`` of a call is one of:

``pushed``           committed and on ``<remote>/<branch>``.
``committed_local``  committed; pushing is disabled (``git_push=false``, the public default).
``written``          file written; git is disabled (``git_commit=false``).
``unchanged``        the requested state already holds; nothing written.
``rolled_back``      a step after the write failed; the commit and file were undone.
``refused``          a precondition or input check failed; nothing was written.
``stranded``         the push failed and the rollback was unsafe; see ``reason``.

``reason`` describes **this call only**.

**What this replaces.** v0.2 degraded any guard refusal to a pushed *review branch*, and left
its commit on local ``main``. Every later call's push range then contained that commit, so
every later call degraded too, with a ``reason`` describing the earlier one, and reported
``pushed: false`` while it had in fact pushed a branch. Five review branches were produced
and none was drained by review. Git history on the target branch, the per-decision audit
event and the agent-bus event make every change attributable and revertible after the fact,
which is the review that actually happens. The additive-only rule is no longer a push
blocker for the same reason: non-additive changes now come only from the explicit update and
remove tools, so they are intended, and step 3 holds each tool to its own kind.

**Credentials.** Pushes use a dedicated deploy key, write-scoped to the one repo, with
``IdentitiesOnly=yes`` and no ``SSH_AUTH_SOCK``, so the host user's ambient key cannot be
used even by mistake. No forge API token: a review flow that needed one would hand this
service account-wide reach to save the operator a click.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from .observability import emit_metric

log = structlog.get_logger()

_GIT_TIMEOUT_S = 60

PUSHED = "pushed"
COMMITTED_LOCAL = "committed_local"
WRITTEN = "written"
UNCHANGED = "unchanged"
ROLLED_BACK = "rolled_back"
REFUSED = "refused"
STRANDED = "stranded"


class PushRefused(Exception):
    """A guard refused the push. Carries the caller-facing reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GitTarget:
    """Where and how a config change is committed. Built once per call by the server."""

    repo: Path
    rel: str
    remote: str
    branch: str
    identity_name: str
    identity_email: str
    push: bool
    deploy_key: Path | None
    audit_dir: Path | None


def identity_args(name: str, email: str) -> list[str]:
    """``-c user.name=... -c user.email=...`` for a fixed-argv git call.

    Passed per-invocation rather than written into the repo's config, so this never
    changes the identity of a commit made by anyone else working in the same checkout.
    """
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


# ---------------------------------------------------------------------------
# Change classification
# ---------------------------------------------------------------------------


def _topics(services: dict, name: str) -> dict[str, dict]:
    return {
        e["topic"]: e for e in (services.get(name) or []) if isinstance(e, dict) and "topic" in e
    }


def classify_change(before: dict | None, after: dict | None) -> str:
    """``additive``, ``repoint``, ``remove``, ``mixed`` or ``none``.

    ``additive``: services or topics appear, nothing existing changes. ``repoint``: an
    existing topic's entry changes (its URL, or its informational trust fields). ``remove``:
    a topic or service disappears. More than one kind at once is ``mixed`` — no tool
    performs that, so it is always rolled back.
    """
    b = (before or {}).get("services") or {}
    a = (after or {}).get("services") or {}
    kinds: set[str] = set()
    for name in set(b) | set(a):
        bt, at = _topics(b, name), _topics(a, name)
        if name in b and name not in a:
            kinds.add("remove")
            continue
        if set(bt) - set(at):
            kinds.add("remove")
        if set(at) - set(bt):
            kinds.add("additive")
        if any(bt[t] != at[t] for t in set(bt) & set(at)):
            kinds.add("repoint")
    if not kinds:
        return "none"
    return kinds.pop() if len(kinds) == 1 else "mixed"


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _ssh_command(deploy_key: Path) -> str:
    """ssh invocation pinned to the deploy key.

    ``IdentitiesOnly=yes`` is the load-bearing option: without it ssh offers every key the
    agent and the default paths provide, so a misconfigured deploy key would silently
    succeed using the host user's ambient credentials. ``BatchMode=yes`` so a prompt is a
    fast failure rather than a hung daemon.
    """
    # Quoted because git runs GIT_SSH_COMMAND through a shell. The path is deployment
    # config, never a caller parameter, so this is not an injection fix — it just removes
    # the standing assumption that the configured path contains no whitespace.
    return (
        f"ssh -i {shlex.quote(str(deploy_key))} -o IdentitiesOnly=yes "
        "-o BatchMode=yes -o StrictHostKeyChecking=yes"
    )


#: Environment git is given. An explicit allowlist rather than ``dict(os.environ)``
#: (SC-06): this process is a long-lived daemon whose environment accumulates whatever the
#: deployment puts there, and git spawns ssh, which reads it.
#:
#: SSH_AUTH_SOCK is deliberately absent, so the deploy key is the only credential that
#: *can* be used. Same reasoning for any ambient forge token.
_GIT_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TZ", "GIT_CONFIG_GLOBAL")


def _git_env(deploy_key: Path | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _GIT_ENV_ALLOWLIST}
    if deploy_key is not None:
        env["GIT_SSH_COMMAND"] = _ssh_command(deploy_key)
    return env


def _git(
    repo: Path, args: list[str], deploy_key: Path | None = None
) -> subprocess.CompletedProcess:
    """Run git with a fixed argv and a minimal environment. Never a shell."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        env=_git_env(deploy_key),
    )


def _rev(repo: Path, ref: str) -> str | None:
    r = _git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return r.stdout.strip() if r.returncode == 0 else None


def _read_yaml_at(repo: Path, ref: str, path: str) -> dict | None:
    """Parse ``<ref>:<path>`` as YAML. None if it cannot be read or parsed."""
    shown = _git(repo, ["show", f"{ref}:{path}"])
    if shown.returncode != 0:
        return None
    try:
        loaded = yaml.safe_load(shown.stdout)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


# ---------------------------------------------------------------------------
# Step 1 — precondition
# ---------------------------------------------------------------------------


def precondition(t: GitTarget) -> str | None:
    """Return a refusal reason, or None if the checkout is fit to take this call's commit.

    Runs before the config file is written. Fail-closed: a check that cannot be evaluated
    is a refusal.
    """
    try:
        status = _git(t.repo, ["status", "--porcelain", "--", t.rel])
        if status.returncode != 0:
            log.error("precondition_status_failed", stderr=status.stderr.strip())
            return f"cannot read git status of {t.rel}"
        if status.stdout.strip():
            return (
                f"{t.rel} has uncommitted changes in the checkout; they must be committed or "
                "discarded (sysadmin) before this tool can change it"
            )
        if not t.push:
            return None

        if t.deploy_key is None or not t.deploy_key.exists():
            log.error("precondition_no_deploy_key", path=str(t.deploy_key))
            return "deploy key not found (path in the server log)"
        head_ref = _git(t.repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        if head_ref.returncode != 0 or head_ref.stdout.strip() != t.branch:
            on = head_ref.stdout.strip() or "a detached HEAD"
            return f"the checkout is on {on}, not {t.branch}; sysadmin must check out {t.branch}"
        fetch = _git(t.repo, ["fetch", "--quiet", t.remote, t.branch], deploy_key=t.deploy_key)
        if fetch.returncode != 0:
            log.error("precondition_fetch_failed", stderr=fetch.stderr.strip())
            return f"cannot fetch {t.remote}/{t.branch} to check the checkout is level with it"
        counts = _git(
            t.repo, ["rev-list", "--left-right", "--count", f"{t.remote}/{t.branch}...HEAD"]
        )
        if counts.returncode != 0:
            log.error("precondition_revlist_failed", stderr=counts.stderr.strip())
            return f"cannot compare {t.branch} with {t.remote}/{t.branch}"
        behind, ahead = (int(x) for x in counts.stdout.split())
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log.error("precondition_error", error=str(exc))
        return "could not evaluate the git precondition"

    if behind or ahead:
        parts = []
        if behind:
            parts.append(f"{behind} behind")
        if ahead:
            parts.append(f"{ahead} ahead of")
        where = " and ".join(parts)
        if not ahead:
            where += " of"
        return (
            f"local {t.branch} is {where} {t.remote}/{t.branch}; sysadmin must reconcile "
            f"{t.repo.name} (pull, or push/drop the unpushed commits) before this tool can "
            "commit to it"
        )
    return None


# ---------------------------------------------------------------------------
# Guards on the push range
# ---------------------------------------------------------------------------


def inspect_push_range(
    repo: Path,
    *,
    remote: str,
    branch: str,
    allowed_path: str,
    identity_email: str,
    deploy_key: Path | None,
) -> list[str]:
    """Return the commits about to be pushed, newest first, or raise `PushRefused`.

    ``commits[0]`` is the tip that was validated — the caller must push *that SHA*, not
    ``HEAD``. Fetches first, so the range is computed against the remote as it is now.
    """
    fetch = _git(repo, ["fetch", "--quiet", remote, branch], deploy_key=deploy_key)
    if fetch.returncode != 0:
        log.error("push_fetch_failed", stderr=fetch.stderr.strip())
        raise PushRefused(f"cannot fetch {remote}/{branch} to evaluate the push range")

    rng = f"{remote}/{branch}..HEAD"

    # No foreign commits: every commit in the range is the tool's.
    authors = _git(repo, ["log", "--format=%H %ae", rng])
    if authors.returncode != 0:
        log.error("push_log_failed", stderr=authors.stderr.strip())
        raise PushRefused("cannot list commits in the push range")

    commits: list[str] = []
    foreign: list[str] = []
    for line in authors.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, email = line.partition(" ")
        commits.append(sha)
        if email.strip() != identity_email:
            foreign.append(f"{sha[:8]} by {email.strip()}")

    if not commits:
        raise PushRefused("nothing to push")
    if foreign:
        raise PushRefused(
            f"push range contains {len(foreign)} commit(s) not authored by this tool: "
            + ", ".join(foreign[:5])
        )

    # Path-only: the range touches the config file and nothing else.
    names = _git(repo, ["diff", "--name-only", rng])
    if names.returncode != 0:
        log.error("push_diff_failed", stderr=names.stderr.strip())
        raise PushRefused("cannot list paths in the push range")

    touched = {p.strip() for p in names.stdout.splitlines() if p.strip()}
    disallowed = touched - {allowed_path}
    if disallowed:
        raise PushRefused(
            f"push range touches {len(disallowed)} path(s) outside {allowed_path!r}: "
            + ", ".join(sorted(disallowed)[:5])
        )
    return commits


# ---------------------------------------------------------------------------
# Audit trail outside git
# ---------------------------------------------------------------------------


def emit_audit_event(
    audit_dir: Path | None,
    summary: str,
    metadata: dict,
    *,
    event: str = "artifact.untracked",
) -> str | None:
    """Append one agent-bus-shaped event. Returns an error string, or None on success.

    Deliberately not fatal: refusing a validated change because a log file is unwritable
    would trade a real capability for a bookkeeping failure. But a failure is never silent
    either — the caller surfaces it in its result, it is logged at ERROR, and it increments
    ``doc_cache_audit_sink_errors_total`` so an alert can see it (vikunja#380).

    No-op when unconfigured, so the public package has no forge-specific behaviour by
    default and CI needs no fixture.
    """
    if audit_dir is None:
        return None
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        path = audit_dir / f"{now.strftime('%Y-%m-%d')}-cross-agent.jsonl"
        record = {
            "id": str(uuid.uuid4()),
            "ts": now.isoformat(),
            "event": event,
            "scope": "cross-agent",
            "source": "doc-cache-mcp",
            "target": None,
            "artifact_path": None,
            "summary": summary,
            "hostname": os.uname().nodename,
            "metadata": metadata,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return None
    except OSError as exc:
        log.error("audit_sink_write_failed", error=str(exc), audit_event=event)
        emit_metric("doc_cache_audit_sink_errors_total", {"event": event}, {"count": 1})
        return f"audit event not written: {exc}"


# ---------------------------------------------------------------------------
# Steps 2-5 — write, commit, classify, guard, push, roll back
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # atomic within the same directory


def _restore_file(t: GitTarget, ref: str) -> bool:
    r = _git(t.repo, ["restore", f"--source={ref}", "--staged", "--worktree", "--", t.rel])
    if r.returncode != 0:
        log.error("restore_failed", stderr=r.stderr.strip())
    return r.returncode == 0


def _rollback(t: GitTarget, pre: str, sha: str) -> str | None:
    """Undo this call's commit. Returns None on success, or why it was not safe to.

    Never discards anyone else's work: the ref moves only by compare-and-swap from exactly
    ``sha`` to exactly ``pre``, and the file is restored only if it still holds what this
    call committed. ``sha`` is re-checked as the tool's own commit first.
    """
    try:
        author = _git(t.repo, ["log", "-1", "--format=%ae", sha])
        if author.returncode != 0 or author.stdout.strip() != t.identity_email:
            return f"commit {sha[:8]} is not this tool's; not rolled back"
        if _rev(t.repo, f"{sha}^") != pre:
            return f"commit {sha[:8]} is not on top of {pre[:8]}; not rolled back"
        swap = _git(
            t.repo,
            ["update-ref", "-m", f"doc-cache-mcp: roll back {sha[:8]}", "HEAD", pre, sha],
        )
        if swap.returncode != 0:
            log.error("rollback_cas_failed", stderr=swap.stderr.strip())
            return (
                f"HEAD moved past {sha[:8]} (a concurrent commit); not rolled back, so "
                f"{sha[:8]} is an unpushed commit on local {t.branch}"
            )
        edited = _git(t.repo, ["diff", "--quiet", sha, "--", t.rel])
        if edited.returncode != 0:
            return (
                f"ref rolled back to {pre[:8]}, but {t.rel} was edited after this call wrote "
                "it, so the file was left as it is"
            )
        if not _restore_file(t, pre):
            return f"ref rolled back to {pre[:8]}, but {t.rel} could not be restored"
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("rollback_error", error=str(exc))
        return "rollback did not complete (git did not run to completion)"
    return None


def _undo(t: GitTarget, pre: str, sha: str, reason: str, base: dict) -> dict:
    problem = _rollback(t, pre, sha)
    if problem is None:
        return {**base, "outcome": ROLLED_BACK, "sha": None, "reason": reason}
    return {**base, "outcome": STRANDED, "sha": sha, "reason": f"{reason}; {problem}"}


def _remote_has(t: GitTarget, sha: str) -> bool | None:
    """After an ambiguous push: did ``sha`` land? None if that cannot be determined."""
    try:
        fetch = _git(t.repo, ["fetch", "--quiet", t.remote, t.branch], deploy_key=t.deploy_key)
        if fetch.returncode != 0:
            return None
        return _rev(t.repo, f"{t.remote}/{t.branch}") == sha
    except (subprocess.SubprocessError, OSError):
        return None


def commit_change(
    t: GitTarget,
    *,
    new_text: str,
    message: str,
    expected: str,
    service: str,
    calling_agent: str | None = None,
) -> dict:
    """Write ``new_text`` to the config, commit it, and push it — or undo it.

    The caller has already run :func:`precondition` under the same lock. ``expected`` is the
    one kind of change (see :func:`classify_change`) the calling tool performs; anything
    else is rolled back. **Does not raise**: every git call is wrapped, because the result
    must describe what is actually on disk.

    Returns ``{"outcome", "sha", "change", "reason"}`` plus ``audit_error`` if the audit
    sink failed.
    """
    path = t.repo / t.rel
    base: dict[str, Any] = {"change": None}
    try:
        pre = _rev(t.repo, "HEAD")
        if pre is None:
            return {**base, "outcome": REFUSED, "sha": None, "reason": "cannot resolve HEAD"}
        _atomic_write(path, new_text)
        ident = identity_args(t.identity_name, t.identity_email)
        add = _git(t.repo, ["add", "--", t.rel])
        commit = (
            _git(t.repo, [*ident, "commit", "-m", message, "--", t.rel])
            if add.returncode == 0
            else add
        )
        if commit.returncode != 0:
            out = (commit.stdout + commit.stderr).lower()
            if "nothing to commit" in out or "no changes added" in out:
                return {**base, "outcome": UNCHANGED, "sha": None, "reason": None}
            log.error("git_commit_failed", stderr=commit.stderr.strip())
            restored = _restore_file(t, "HEAD")
            return {
                **base,
                "outcome": ROLLED_BACK if restored else STRANDED,
                "sha": None,
                "reason": "git commit failed"
                + ("" if restored else f"; {t.rel} could not be restored"),
            }
        sha = _rev(t.repo, "HEAD")
        if sha is None:
            return {
                **base,
                "outcome": STRANDED,
                "sha": None,
                "reason": "commit made, HEAD unreadable",
            }
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("commit_error", error=str(exc))
        return {
            **base,
            "outcome": STRANDED,
            "sha": None,
            "reason": "git did not run to completion",
        }

    try:
        change = classify_change(
            _read_yaml_at(t.repo, pre, t.rel), _read_yaml_at(t.repo, sha, t.rel)
        )
    except (subprocess.SubprocessError, OSError):
        change = "unknown"
    base["change"] = change

    def done(result: dict) -> dict:
        return _audited(t, result, service, calling_agent)

    if change != expected:
        reason = f"change classified as {change!r}, but this tool only makes {expected!r}"
        return done(_undo(t, pre, sha, reason, base))

    if not t.push:
        return done(
            {
                **base,
                "outcome": COMMITTED_LOCAL,
                "sha": sha,
                "reason": "push disabled (git_push=false)",
            }
        )

    try:
        commits = inspect_push_range(
            t.repo,
            remote=t.remote,
            branch=t.branch,
            allowed_path=t.rel,
            identity_email=t.identity_email,
            deploy_key=t.deploy_key,
        )
    except PushRefused as refusal:
        return done(_undo(t, pre, sha, refusal.reason, base))
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("push_guard_error", error=str(exc))
        return done(_undo(t, pre, sha, "could not evaluate push guards", base))

    tip = commits[0]
    failure = "push to the target branch was rejected"
    try:
        pushed = _git(
            t.repo, ["push", t.remote, f"{tip}:refs/heads/{t.branch}"], deploy_key=t.deploy_key
        )
        ok = pushed.returncode == 0
        if not ok:
            log.error("push_failed", stderr=pushed.stderr.strip())
    except (subprocess.SubprocessError, OSError) as exc:
        # Notably TimeoutExpired, where the push may have landed. Ask the remote.
        log.error("push_subprocess_error", error=str(exc))
        landed = _remote_has(t, tip)
        if landed is None:
            return done(
                {
                    **base,
                    "outcome": STRANDED,
                    "sha": sha,
                    "reason": "push did not complete and the remote could not be checked; "
                    f"{sha[:8]} may or may not be on {t.remote}/{t.branch}",
                }
            )
        ok = landed
        failure = "push did not complete and the commit is not on the remote"

    if not ok:
        return done(_undo(t, pre, sha, failure, base))
    return done({**base, "outcome": PUSHED, "sha": tip, "reason": None})


def _audited(t: GitTarget, result: dict, service: str, calling_agent: str | None) -> dict:
    """Record the decision outside git, so the trail survives a force-push."""
    outcome = result["outcome"]
    err = emit_audit_event(
        t.audit_dir,
        f"doc-cache-mcp {outcome} a {result.get('change')} change for service {service!r}"
        + (f": {result['reason']}" if result.get("reason") else ""),
        {
            "service": service,
            "outcome": outcome,
            "change": result.get("change"),
            "sha": result.get("sha"),
            "reason": result.get("reason"),
            "branch": t.branch,
            "path": t.rel,
            "calling_agent": calling_agent,
        },
    )
    if err:
        result["audit_error"] = err
    return result

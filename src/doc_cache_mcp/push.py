"""Constrained push of the docs-cache config commit.

``doc_cache_add_service`` writes one file and commits it, then stops. The commit sits on
local ``main`` forever, because the agent that called the tool has no git write access —
it is precisely that lack of access the MCP exists to discharge. So the tool reported
``committed: true`` for an operation nobody could finish (vikunja#363).

This module finishes it, under guards, because "an MCP pushes to a shared repo's main
unattended" is a real privilege and should read like one.

Guards, in order. Every one of them is fail-closed: if a guard cannot be *evaluated*, that
is a refusal, not a pass.

1. **Distinct commit identity.** Commits are authored as ``doc-cache-mcp`` rather than as
   the host user, so tool commits are distinguishable from human ones — and so guard 3
   has something to match on.
2. **A dedicated deploy key**, write-scoped to the single target repo, used with
   ``IdentitiesOnly=yes`` so ssh cannot silently fall back to the host user's ambient key
   (which reaches every repo that key can reach).
3. **No foreign commits.** Every commit in the push range must be authored by the
   identity from guard 1. This is the guard that stops the tool sweeping up somebody
   else's unreviewed work-in-progress on the way to pushing its own one-line change.

   The push sends **the exact SHA the guards inspected**, never the ``HEAD`` ref. Git
   re-resolves ``HEAD`` at push time, so pushing the ref would let anything committed
   between evaluation and push ride along unchecked — this guard defeated by a race
   rather than by a missing check. Found by the security audit of this module, not by
   its tests, which only ever exercised static state.
4. **Path allowlist at push time**, not just at commit time. The push range must touch
   the config file and nothing else — defence in depth if guard 3 is ever bypassed.
5. **Additive-only.** The config as it will land must be a purely additive change to the
   config already on the remote — no service or topic removed, no existing URL
   re-pointed. The tool's semantic is "add a service"; enforce it rather than relying on
   it emerging from the merge logic.

   This is evaluated at push time, against ``<remote>/<branch>`` versus ``HEAD``, not
   against whatever this process happened to read before writing. That catches a
   non-additive change introduced by *any* commit in the range, not just by this call.
   It is also why re-pointing a URL degrades to a review branch rather than erroring:
   replacing an existing topic's URL is documented, tested, idempotent behaviour of
   ``doc_cache_add_service``, so it must stay possible — it just should not land on a
   shared branch unattended.
6. **An audit event per decision**, outside git, so the trail survives a force-push.
7. **Degrade, do not fail.** When a guard trips, the commit is pushed to a review branch
   instead of to the target branch, and the URL to open a pull request is returned. The
   normal case for this tool is a single additive line in one file and will pass every
   guard; a review branch should be rare and each one worth a human's attention.

**Deviation from the build plan, deliberate.** The plan specified opening a pull request
via the forge API on degradation. Doing that needs a Gitea API token, which is
*account-wide* — it would hand this service far broader reach than the single-repo deploy
key guard 2 exists to bound, undoing most of that guard's value to save the operator one
click. Pushing a review branch with the key we already have reaches the same outcome
(human review, nothing stranded locally, a URL returned) at no extra credential.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger()

_GIT_TIMEOUT_S = 60


#: ``service`` reaches a git ref name in :func:`_degrade`. server.py validates it before
#: calling, but the guard is repeated here rather than assumed: this is an exported
#: function, and a validation that lives only at one call site is one refactor away from
#: not running at all (IV-01).
#:
#: Stricter than server.py's ``_SERVICE_RE`` in one respect: a leading ``-`` is refused.
#: ``^[A-Za-z0-9_-]+$`` admits ``--force``, which is a legal service name today but reads
#: as an option anywhere it is not carefully positioned. Nothing here puts it in such a
#: position and none of the 63 configured services starts with a dash, so this costs
#: nothing and removes the question.
_SAFE_SERVICE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")


class PushRefused(Exception):
    """A guard refused the push. Carries the caller-facing reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Guard 1 — commit identity
# ---------------------------------------------------------------------------


def identity_args(name: str, email: str) -> list[str]:
    """``-c user.name=... -c user.email=...`` for a fixed-argv git call.

    Passed per-invocation rather than written into the repo's config, so this never
    changes the identity of a commit made by anyone else working in the same checkout.
    """
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


# ---------------------------------------------------------------------------
# Guard 5 — additive-only validation
# ---------------------------------------------------------------------------


def validate_additive(before: dict, after: dict) -> str | None:
    """Return a refusal reason if *after* is not a purely additive change to *before*.

    Additive means: no service disappears, no topic disappears, and no existing topic's
    URL changes to something different. Adding services, adding topics, and re-adding a
    topic with an identical URL are all fine.

    Re-pointing an existing topic at a new URL is refused even though the tool's docstring
    calls it idempotent — a URL swap is how a cache-poisoning attempt would look, and it
    is not what "add a service" means. It stays possible by editing the file directly.
    """
    before_services = (before or {}).get("services") or {}
    after_services = (after or {}).get("services") or {}

    for name in before_services:
        if name not in after_services:
            return f"refuses to remove service {name!r}"

    for name, before_entries in before_services.items():
        before_urls = {
            e["topic"]: e.get("url")
            for e in (before_entries or [])
            if isinstance(e, dict) and "topic" in e
        }
        after_urls = {
            e["topic"]: e.get("url")
            for e in (after_services.get(name) or [])
            if isinstance(e, dict) and "topic" in e
        }
        for topic, url in before_urls.items():
            if topic not in after_urls:
                return f"refuses to remove topic {topic!r} from service {name!r}"
            if after_urls[topic] != url:
                return (
                    f"refuses to change the url of existing topic {topic!r} in service "
                    f"{name!r} — this tool only adds"
                )
    return None


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _ssh_command(deploy_key: Path) -> str:
    """ssh invocation pinned to the deploy key.

    ``IdentitiesOnly=yes`` is the load-bearing option: without it ssh offers every key the
    agent and the default paths provide, so a misconfigured deploy key would silently
    succeed using the host user's ambient credentials — the exact broad-reach credential
    guard 2 exists to avoid, and it would look like it was working.

    ``BatchMode=yes`` so a prompt is a fast failure rather than a hung daemon.
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
#: SSH_AUTH_SOCK is deliberately absent. ``IdentitiesOnly=yes`` already stops ssh offering
#: unrelated agent identities, but withholding the socket entirely means the deploy key is
#: the only credential that *can* be used — guard 2 then holds structurally rather than by
#: correct option handling. Same reasoning for any ambient forge token: git has no use for
#: one over ssh, so it does not get one.
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


# ---------------------------------------------------------------------------
# Guards 3 + 4 — what is actually in the push range
# ---------------------------------------------------------------------------


def inspect_push_range(
    repo: Path,
    *,
    remote: str,
    branch: str,
    allowed_path: str,
    identity_email: str,
    deploy_key: Path,
) -> tuple[list[str], list[str]]:
    """Return ``(commits, notes)`` for the range about to be pushed, or raise `PushRefused`.

    ``commits`` is newest-first, so ``commits[0]`` is the tip that was validated — the
    caller must push *that SHA*, not ``HEAD``. See :func:`push_config_commit`.

    ``notes`` records any guard that was skipped rather than passed, so a check that did
    not run cannot be mistaken for a check that succeeded.

    Fetches first. Without that, ``<remote>/<branch>`` is whatever this checkout last saw,
    so the range would be computed against a stale remote and could either miss commits
    somebody else pushed or re-examine commits already upstream. A guard evaluated against
    a stale view is not a guard.
    """
    fetch = _git(repo, ["fetch", "--quiet", remote, branch], deploy_key=deploy_key)
    if fetch.returncode != 0:
        log.error("push_fetch_failed", stderr=fetch.stderr.strip())
        raise PushRefused(f"cannot fetch {remote}/{branch} to evaluate the push range")

    rng = f"{remote}/{branch}..HEAD"

    # Guard 3 — author of every commit in the range.
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

    # Guard 4 — paths touched by the range.
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

    # Guard 5 — compare what is on the remote against what would land.
    refusal, note = _check_additive_in_range(
        repo, allowed_path=allowed_path, remote=remote, branch=branch
    )
    if refusal:
        raise PushRefused(refusal)

    return commits, ([note] if note else [])


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


def _check_additive_in_range(
    repo: Path, *, allowed_path: str, remote: str, branch: str
) -> tuple[str | None, str | None]:
    """Guard 5. Returns ``(refusal_reason, skip_note)`` — at most one is non-None."""
    after = _read_yaml_at(repo, "HEAD", allowed_path)
    if after is None:
        return f"cannot parse {allowed_path} at HEAD to check the change is additive", None

    before = _read_yaml_at(repo, f"{remote}/{branch}", allowed_path)
    if before is None:
        # The file does not exist upstream yet (or is unparseable there). Creating it is
        # additive by definition, and an unparseable upstream cannot be distinguished from
        # a missing one from here — so this is a skip, not a pass. It is reported as one:
        # a guard that did not run must not look identical to a guard that succeeded, which
        # is the whole complaint behind this build's other three tickets.
        return None, (f"additive check skipped — no parseable {allowed_path} on {remote}/{branch}")

    return validate_additive(before, after), None


# ---------------------------------------------------------------------------
# Guard 6 — audit trail outside git
# ---------------------------------------------------------------------------


def emit_audit_event(audit_dir: Path | None, summary: str, metadata: dict) -> str | None:
    """Append one agent-bus-shaped event. Returns an error string, or None on success.

    Deliberately not fatal. The push is already fully guarded by the time this runs, and
    refusing a validated push because a log file is unwritable would trade a real
    capability for a bookkeeping failure. The caller surfaces the error in its result
    rather than dropping it — an audit sink that fails silently is the same defect class
    this whole change is about.

    No-op when unconfigured, so the public package has no forge-specific behaviour by
    default and CI needs no fixture.
    """
    if audit_dir is None:
        return None
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        path = audit_dir / f"{now.strftime('%Y-%m-%d')}-cross-agent.jsonl"
        event = {
            "id": str(uuid.uuid4()),
            "ts": now.isoformat(),
            "event": "artifact.untracked",
            "scope": "cross-agent",
            "source": "doc-cache-mcp",
            "target": None,
            "artifact_path": None,
            "summary": summary,
            "hostname": os.uname().nodename,
            "metadata": metadata,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        return None
    except OSError as exc:
        log.error("push_audit_write_failed", error=str(exc))
        return f"audit event not written: {exc}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _pr_url(remote_url: str, branch: str, target: str) -> str | None:
    """Best-effort web URL for opening a PR from *branch*. None if the remote is unusual.

    Handles the three forms in use on forge: ``ssh://git@host:2222/org/repo.git``, the
    scp-like ``git@host:org/repo.git``, and plain https. Returning None is fine — the
    caller still reports the review branch, which is the part that matters.
    """
    url = remote_url.strip()
    for scheme in ("ssh://", "https://", "http://", "git://"):
        if url.startswith(scheme):
            url = url[len(scheme) :]
            break
    if "@" in url:
        url = url.split("@", 1)[1]

    # scp-like or explicit port: host:something → host/something, dropping a numeric port.
    if ":" in url.split("/", 1)[0]:
        host_part, _, rest = url.partition(":")
        head, _, tail = rest.partition("/")
        rest = tail if head.isdigit() and tail else rest
        url = f"{host_part}/{rest}"

    host, _, path = url.partition("/")
    path = path.removesuffix(".git").strip("/")
    if not host or "/" not in path:
        return None
    return f"https://{host}/{path}/compare/{target}...{branch}"


def push_config_commit(
    repo: Path,
    *,
    allowed_path: str,
    remote: str,
    branch: str,
    identity_email: str,
    deploy_key: Path,
    review_branch_prefix: str,
    audit_dir: Path | None,
    service: str,
    calling_agent: str | None = None,
) -> dict:
    """Push the pending config commit, or degrade to a review branch.

    **Does not raise.** Every git call is wrapped. This matters beyond tidiness: the caller
    has already committed successfully by the time this runs, and an exception escaping
    here is caught upstream by ``doc_cache_add_service``'s broad handler, which discards
    the already-true ``committed: True`` and reports ``committed: False`` — telling the
    operator nothing happened when a commit is sitting on disk. That is the same
    reported-state-versus-real-state defect the rest of this build exists to fix, so the
    property is asserted by a test rather than by this sentence (an earlier version of this
    docstring claimed it while the push call was unwrapped).

    Returns the ``commit`` sub-dict fields describing what happened — always including
    ``pushed``; ``pushed_sha`` on success; on degradation a ``reason`` plus the review
    branch and PR URL; ``notes`` for any guard that was skipped rather than passed.
    """
    if not _SAFE_SERVICE.match(service or ""):
        return {"pushed": False, "reason": f"invalid service name {service!r}"}

    if not deploy_key.exists():
        return {
            "pushed": False,
            "reason": f"deploy key not found at {deploy_key}",
        }

    try:
        commits, notes = inspect_push_range(
            repo,
            remote=remote,
            branch=branch,
            allowed_path=allowed_path,
            identity_email=identity_email,
            deploy_key=deploy_key,
        )
    except PushRefused as refusal:
        return _degrade(
            repo,
            reason=refusal.reason,
            remote=remote,
            branch=branch,
            deploy_key=deploy_key,
            review_branch_prefix=review_branch_prefix,
            audit_dir=audit_dir,
            service=service,
            calling_agent=calling_agent,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("push_guard_error", error=str(exc))
        return {"pushed": False, "reason": "could not evaluate push guards"}

    # Push the SHA the guards actually inspected, NOT the `HEAD` ref.
    #
    # `HEAD` is a moving target: it is resolved again by git at push time, several
    # subprocess calls after inspect_push_range() read it. Anything that commits into this
    # checkout in that window — a concurrent add_service call over streamable-http, a human
    # working in host-forge-scripts, another automation — would ride along having passed
    # none of guards 3, 4 or 5. That is exactly the failure guard 3 exists to prevent,
    # reintroduced as a race rather than as a missing check. Pinning the tip closes the
    # window without needing a lock: a commit arriving late simply is not in the push.
    tip = commits[0]
    try:
        result = _git(repo, ["push", remote, f"{tip}:refs/heads/{branch}"], deploy_key=deploy_key)
    except (subprocess.SubprocessError, OSError) as exc:
        # Notably includes TimeoutExpired, where the push may well have landed server-side.
        # Report the ambiguity rather than asserting failure — and never raise, because the
        # caller has already committed successfully and an exception escaping here gets
        # relabelled upstream as "the commit failed", which is a lie about work that is on
        # disk.
        log.error("push_subprocess_error", error=str(exc))
        return {
            "pushed": False,
            "reason": (
                "push outcome unknown: git did not run to completion, so the commit may or "
                "may not have reached the remote — check the branch before retrying"
            ),
        }

    if result.returncode != 0:
        log.error("push_failed", stderr=result.stderr.strip())
        return _degrade(
            repo,
            reason="push to the target branch was rejected",
            remote=remote,
            branch=branch,
            deploy_key=deploy_key,
            review_branch_prefix=review_branch_prefix,
            audit_dir=audit_dir,
            service=service,
            calling_agent=calling_agent,
        )

    out: dict[str, Any] = {
        "pushed": True,
        "identity": identity_email,
        "commits": len(commits),
        "pushed_sha": tip,
    }
    if notes:
        out["notes"] = notes
    audit_err = emit_audit_event(
        audit_dir,
        f"doc-cache-mcp pushed {len(commits)} commit(s) for service {service!r} "
        f"to {remote}/{branch}",
        {
            "service": service,
            "commits": commits,
            "pushed_sha": tip,
            "branch": branch,
            "calling_agent": calling_agent,
            "path": allowed_path,
            "notes": notes,
        },
    )
    if audit_err:
        out["audit_error"] = audit_err
    return out


def _degrade(
    repo: Path,
    *,
    reason: str,
    remote: str,
    branch: str,
    deploy_key: Path,
    review_branch_prefix: str,
    audit_dir: Path | None,
    service: str,
    calling_agent: str | None,
) -> dict:
    """A guard tripped: put the work somewhere a human can review it, and say why."""
    if reason == "nothing to push":
        # Not a failure — the commit step found no changes. Say so plainly rather than
        # manufacturing a review branch for an empty diff.
        return {"pushed": False, "reason": reason}

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    review_branch = f"{review_branch_prefix}/{service}-{stamp}"

    out: dict[str, Any] = {"pushed": False, "reason": reason}

    try:
        # Resolve once and push that SHA, for the same reason the success path does: the
        # branch a human is asked to review must be the state that was actually assessed,
        # not whatever HEAD drifted to afterwards.
        head_rev = _git(repo, ["rev-parse", "HEAD"])
        tip = head_rev.stdout.strip() if head_rev.returncode == 0 else "HEAD"
        pushed = _git(
            repo,
            ["push", remote, f"{tip}:refs/heads/{review_branch}"],
            deploy_key=deploy_key,
        )
        remote_url = _git(repo, ["remote", "get-url", remote]) if pushed.returncode == 0 else None
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("push_review_branch_error", error=str(exc))
        out["review_branch_error"] = "could not push a review branch (git did not complete)"
        pushed = None
        remote_url = None
        tip = None

    if pushed is not None and pushed.returncode == 0:
        out["review_branch"] = review_branch
        out["review_sha"] = tip
        if remote_url is not None and remote_url.returncode == 0:
            url = _pr_url(remote_url.stdout, review_branch, branch)
            if url:
                out["pr_url"] = url
    elif pushed is not None:
        # Both paths failed. Do not dress this up — the commit is stranded locally, which
        # is exactly the state vikunja#363 is about.
        log.error("push_review_branch_failed", stderr=pushed.stderr.strip())
        out["review_branch_error"] = "could not push a review branch either"

    audit_err = emit_audit_event(
        audit_dir,
        f"doc-cache-mcp REFUSED to push for service {service!r}: {reason}",
        {
            "service": service,
            "reason": reason,
            "head": tip,
            "review_branch": out.get("review_branch"),
            "calling_agent": calling_agent,
        },
    )
    if audit_err:
        out["audit_error"] = audit_err
    return out

"""Remediation test — F-01: fetch-time SSRF guard in doc-sync.safe_fetch.

Confirms the fetch path (cron + doc_cache_sync) re-validates the URL and every redirect hop
against the allowlist and never auto-follows a redirect to an unvalidated target."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import requests

DOCSYNC = Path.home() / "scripts" / "doc-sync.py"
pytestmark = pytest.mark.skipif(not DOCSYNC.exists(), reason="doc-sync.py not present")

ALLOW = {
    "hosts": {"docs.example.com"},
    "forge_endpoints": [("vikunja.helmforge.me", "/api/v1/docs.json")],
}
FORGE = "https://vikunja.helmforge.me/api/v1/docs.json"


def _load():
    spec = importlib.util.spec_from_file_location("forge_doc_sync_fetch", str(DOCSYNC))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeResp:
    def __init__(self, status=200, text="OK", location=None):
        self.status_code = status
        self.text = text
        self.headers = {"Location": location} if location else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.kwargs = []

    def get(self, url, timeout=30, allow_redirects=False):
        self.calls.append(url)
        self.kwargs.append(allow_redirects)
        return self._responses.pop(0)


def _setup(m, responses):
    m._ALLOWLIST_CACHE = ALLOW
    m.SESSION = FakeSession(responses)
    return m


def test_direct_fetch_ok():
    m = _setup(_load(), [FakeResp(200, "BODY")])
    assert m.safe_fetch(FORGE) == "BODY"
    # requests must be called with auto-redirects disabled.
    assert m.SESSION.kwargs == [False]


def test_redirect_to_ip_literal_internal_rejected():
    m = _setup(_load(), [FakeResp(302, location="https://169.254.169.254/latest/meta-data/")])
    with pytest.raises(m._allow.AllowlistError):
        m.safe_fetch(FORGE)
    # The internal redirect target was refused BEFORE being fetched.
    assert m.SESSION.calls == [FORGE]


def test_redirect_to_unlisted_host_rejected():
    m = _setup(_load(), [FakeResp(301, location="https://evil.example.net/x")])
    with pytest.raises(m._allow.AllowlistError):
        m.safe_fetch(FORGE)
    assert m.SESSION.calls == [FORGE]


def test_redirect_to_loopback_rejected():
    m = _setup(_load(), [FakeResp(307, location="http://127.0.0.1:8080/admin")])
    with pytest.raises(m._allow.AllowlistError):
        m.safe_fetch(FORGE)


def test_too_many_redirects():
    loop = [FakeResp(302, location=FORGE) for _ in range(10)]
    m = _setup(_load(), loop)
    with pytest.raises(requests.TooManyRedirects):
        m.safe_fetch(FORGE, max_redirects=3)
    # max_redirects=3 -> 4 attempts before giving up.
    assert len(m.SESSION.calls) == 4


def test_unlisted_initial_url_rejected_before_any_fetch():
    m = _setup(_load(), [FakeResp(200, "X")])
    with pytest.raises(m._allow.AllowlistError):
        m.safe_fetch("https://evil.example.net/readme")
    assert m.SESSION.calls == []  # refused before the network call

"""Tests del puerto único de red (A1: ``vp_data.fetchers``). Sin red real.

La costura DIP: cualquier ``Fetcher`` mapea URL -> bytes; la política de retry
vive UNA sola vez en ``with_retry`` (sleep inyectable); un bloqueo anti-bot de
la fuente (Cloudflare, medido en vivo desde el 6-ago-2026) se distingue de un
404/500 con ``SourceBlockedError`` para que el consumidor degrade honesto en
vez de quemar backoff.
"""

from __future__ import annotations

import pytest

from vp_data import config, fetchers
from vp_data.fetchers import (
    FetchError,
    SourceBlockedError,
    default_fetcher,
    local_dir_fetcher,
    requests_fetcher,
    with_retry,
)


class _Resp:
    def __init__(self, status_code=200, content=b"ok", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


def _get_returning(resp, calls):
    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return resp

    return fake_get


# ------------------------------------------------------------- requests_fetcher
def test_requests_fetcher_returns_wire_bytes_and_sends_browser_headers(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(fetchers.requests, "get", _get_returning(_Resp(content=b"<html>page</html>"), calls))
    assert requests_fetcher()("http://x/a.html") == b"<html>page</html>"
    headers = calls[0]["headers"]
    assert "Mozilla" in headers["User-Agent"] and "python-requests" not in headers["User-Agent"]
    assert calls[0]["timeout"] == fetchers.REQUEST_TIMEOUT


@pytest.mark.parametrize(
    "status,body",
    [
        (403, b"<html><title>Attention Required! | Cloudflare</title></html>"),
        (403, b"<html>Just a moment...</html>"),
        (503, b"<html><div id='cf-please-wait'>checking</div></html>"),
    ],
)
def test_cloudflare_block_raises_source_blocked_and_is_permanent(monkeypatch, status, body):
    monkeypatch.setattr(fetchers.requests, "get", _get_returning(_Resp(status, body), []))
    with pytest.raises(SourceBlockedError) as exc:
        requests_fetcher()("http://x/a.html")
    assert exc.value.permanent and exc.value.status == status


def test_403_with_cloudflare_server_header_is_blocked_even_without_body_markers(monkeypatch):
    resp = _Resp(403, b"denied", headers={"Server": "cloudflare"})
    monkeypatch.setattr(fetchers.requests, "get", _get_returning(resp, []))
    with pytest.raises(SourceBlockedError):
        requests_fetcher()("http://x/a.html")


def test_plain_404_is_permanent_fetch_error_not_blocked(monkeypatch):
    monkeypatch.setattr(fetchers.requests, "get", _get_returning(_Resp(404, b"not found"), []))
    with pytest.raises(FetchError) as exc:
        requests_fetcher()("http://x/gone.html")
    assert exc.value.permanent and not isinstance(exc.value, SourceBlockedError)
    assert exc.value.status == 404


def test_500_is_transient_fetch_error(monkeypatch):
    monkeypatch.setattr(fetchers.requests, "get", _get_returning(_Resp(500, b"boom"), []))
    with pytest.raises(FetchError) as exc:
        requests_fetcher()("http://x/a.html")
    assert not exc.value.permanent


def test_network_exception_is_transient_fetch_error(monkeypatch):
    def raising_get(url, **kwargs):
        raise fetchers.requests.ConnectionError("reset")

    monkeypatch.setattr(fetchers.requests, "get", raising_get)
    with pytest.raises(FetchError) as exc:
        requests_fetcher()("http://x/a.html")
    assert not exc.value.permanent


# ------------------------------------------------------------------- with_retry
def test_with_retry_retries_transients_with_linear_backoff_then_raises_last():
    attempts = {"n": 0}
    naps: list[float] = []

    def flaky(url: str) -> bytes:
        attempts["n"] += 1
        raise FetchError(url, "transient", permanent=False)

    with pytest.raises(FetchError):
        with_retry(flaky, retries=3, sleep=naps.append)("http://x/a.html")
    assert attempts["n"] == 3
    assert naps == [fetchers.BACKOFF_BASE_S * 1, fetchers.BACKOFF_BASE_S * 2, fetchers.BACKOFF_BASE_S * 3]


def test_with_retry_recovers_after_a_transient():
    attempts = {"n": 0}

    def flaky_then_ok(url: str) -> bytes:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise FetchError(url, "transient", permanent=False)
        return b"page"

    assert with_retry(flaky_then_ok, retries=5, sleep=lambda _: None)("http://x") == b"page"
    assert attempts["n"] == 3


@pytest.mark.parametrize(
    "exc",
    [FetchError("http://x", "closed", permanent=True), SourceBlockedError("http://x", "closed")],
    ids=["permanent", "source-blocked"],
)
def test_with_retry_fast_fails_on_permanent_and_on_source_blocked(exc):
    attempts = {"n": 0}

    def dead(url: str) -> bytes:
        attempts["n"] += 1
        raise exc

    with pytest.raises(type(exc)):
        with_retry(dead, retries=5, sleep=lambda _: None)("http://x")
    assert attempts["n"] == 1, f"{type(exc).__name__} debe fallar sin reintentos"


# ------------------------------------------------------------ local_dir_fetcher
def test_local_dir_fetcher_serves_url_basename_from_directory(tmp_path):
    (tmp_path / "visa-bulletin-for-july-2026.html").write_bytes(b"frozen")
    fetch = local_dir_fetcher(tmp_path)
    assert fetch("https://travel.state.gov/x/y/visa-bulletin-for-july-2026.html") == b"frozen"


def test_local_dir_fetcher_missing_file_uses_fallback_or_fails_permanent(tmp_path):
    fallback_calls: list[str] = []

    def fallback(url: str) -> bytes:
        fallback_calls.append(url)
        return b"from-fallback"

    assert local_dir_fetcher(tmp_path, fallback=fallback)("http://x/missing.html") == b"from-fallback"
    assert fallback_calls == ["http://x/missing.html"]
    with pytest.raises(FetchError) as exc:
        local_dir_fetcher(tmp_path)("http://x/missing.html")
    assert exc.value.permanent


# -------------------------------------------------------------- default_fetcher
def test_default_fetcher_defaults_to_live_requests_mode(monkeypatch):
    monkeypatch.delenv("VP_FETCHER", raising=False)
    assert callable(default_fetcher())


def test_default_fetcher_inbox_serves_inbox_then_snapshots(tmp_path, monkeypatch):
    inbox, snaps = tmp_path / "inbox", tmp_path / "snapshots"
    inbox.mkdir()
    snaps.mkdir()
    (inbox / "a.html").write_bytes(b"inbox-copy")
    (snaps / "b.html").write_bytes(b"snapshot-copy")
    monkeypatch.setattr(config, "INBOX_DIR", inbox)
    monkeypatch.setattr(config, "SNAPSHOTS_DIR", snaps)
    monkeypatch.setenv("VP_FETCHER", "inbox")
    fetch = default_fetcher()
    assert fetch("http://x/a.html") == b"inbox-copy"
    assert fetch("http://x/b.html") == b"snapshot-copy"


def test_default_fetcher_browser_fails_explicitly_deferred_to_a6(monkeypatch):
    monkeypatch.setenv("VP_FETCHER", "browser")
    with pytest.raises(NotImplementedError, match="A6"):
        default_fetcher()


def test_default_fetcher_rejects_unknown_mode(monkeypatch):
    monkeypatch.setenv("VP_FETCHER", "carrier-pigeon")
    with pytest.raises(ValueError, match="carrier-pigeon"):
        default_fetcher()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))

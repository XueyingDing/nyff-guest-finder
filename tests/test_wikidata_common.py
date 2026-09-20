"""Regression tests for scripts/_wikidata_common.py.

This is where the original bug lived: get_json() fell through to `return {}` after
exhausting retries against HTTP 429, instead of raising -- so a request that never
actually succeeded was indistinguishable from one that succeeded and legitimately
found nothing. These tests pin down the fixed contract:
  - a successful response (including an empty one) is returned as-is;
  - exhausting retries (429s, timeouts, or a mix) always raises LookupUnavailable,
    never returns a fabricated empty dict;
  - the server's Retry-After header is honoured when present.
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from _wikidata_common import LookupUnavailable, get_json  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Tests never need to actually wait out a backoff."""
    sleeps = []
    monkeypatch.setattr("_wikidata_common.time.sleep", lambda s: sleeps.append(s))
    return sleeps


def test_success_returns_json(monkeypatch):
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append(1)
        return FakeResponse(200, {"search": [{"id": "Q1"}]})

    monkeypatch.setattr("_wikidata_common.requests.get", fake_get)
    result = get_json("http://x", {}, {})
    assert result == {"search": [{"id": "Q1"}]}
    assert len(calls) == 1


def test_success_with_no_matches_is_not_an_error(monkeypatch):
    """A real, successful 'no results' response must come back as data, not raise --
    this is the 'request succeeded but found nothing' case the fix must distinguish
    from 'request never succeeded'."""
    monkeypatch.setattr("_wikidata_common.requests.get",
                         lambda *a, **k: FakeResponse(200, {"search": []}))
    result = get_json("http://x", {}, {})
    assert result == {"search": []}


def test_429_exhausted_raises_lookup_unavailable(monkeypatch, no_real_sleep):
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append(1)
        return FakeResponse(429)

    monkeypatch.setattr("_wikidata_common.requests.get", fake_get)
    with pytest.raises(LookupUnavailable):
        get_json("http://x", {}, {}, retries=3)
    assert len(calls) == 3  # tried exactly `retries` times, then raised -- never fell through to {}


def test_429_then_success_recovers(monkeypatch):
    responses = iter([FakeResponse(429), FakeResponse(429), FakeResponse(200, {"ok": True})])
    monkeypatch.setattr("_wikidata_common.requests.get", lambda *a, **k: next(responses))
    result = get_json("http://x", {}, {}, retries=3)
    assert result == {"ok": True}


def test_timeout_then_success_recovers(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.Timeout("boom")
        return FakeResponse(200, {"ok": True})

    monkeypatch.setattr("_wikidata_common.requests.get", fake_get)
    assert get_json("http://x", {}, {}, retries=3) == {"ok": True}
    assert calls["n"] == 2


def test_all_timeouts_raise_lookup_unavailable(monkeypatch, no_real_sleep):
    monkeypatch.setattr("_wikidata_common.requests.get",
                         lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("boom")))
    with pytest.raises(LookupUnavailable):
        get_json("http://x", {}, {}, retries=3)


def test_retry_after_seconds_header_is_used(monkeypatch, no_real_sleep):
    responses = iter([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {"ok": True})])
    monkeypatch.setattr("_wikidata_common.requests.get", lambda *a, **k: next(responses))
    get_json("http://x", {}, {}, retries=3)
    assert no_real_sleep == [7.0]  # honoured the server's wait time, not the default backoff


def test_no_retry_after_header_falls_back_to_default_backoff(monkeypatch, no_real_sleep):
    responses = iter([FakeResponse(429), FakeResponse(200, {"ok": True})])
    monkeypatch.setattr("_wikidata_common.requests.get", lambda *a, **k: next(responses))
    get_json("http://x", {}, {}, retries=3)
    assert no_real_sleep == [5.0]  # default backoff for attempt 0: 5 * (0 + 1)

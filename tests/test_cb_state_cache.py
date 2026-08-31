"""The circuit-breaker state cache (sdk-spec.md §4.4).

`check()` is a network round trip in front of a sensitive action. The
cache removes it for repeated checks on the same subject, and the whole
reason to be careful is that a cached `closed` is an allow the server
might no longer give.

The rules these tests hold:

  * off unless the caller sets a TTL — a caching safety check nobody
    asked for is worse than a slow one;
  * a served entry always says it was served, and how old it was, so a
    caller recording a denial can tell it read stale state;
  * one TTL for every state: holding a deny longer than an allow is a
    safety policy that belongs to whoever set the TTL;
  * `subject` and `interaction` with the same id are different keys;
  * the map is bounded, because the key is a subject id;
  * errors are never cached, and `last_known` is opt-in, marked, and
    unreachable without a TTL to fall back on.
"""
from __future__ import annotations

import threading
import time

import httpx
import pytest

from dmzagent import DMZAgent
from dmzagent.cb_cache import CBStateCache
from dmzagent.errors import RateLimitError, ServerError
from dmzagent.models import CheckResult

_KEY = "ck_test_cache"


def _closed(reason: str = "no policies fired") -> dict:
    return {"state": "closed", "allow": True, "warning": False, "reason": reason,
            "fired_policies": [], "anchor": None, "checked_at": "2026-08-31T00:00:00Z",
            "latency_ms": 12.3, "route_latency_ms": 18.7}


def _open(reason: str = "policy fired") -> dict:
    return {"state": "open", "allow": False, "warning": False, "reason": reason,
            "fired_policies": [{"cb_policy_id": "p1", "name": "n", "action": "block"}],
            "anchor": None, "checked_at": "2026-08-31T00:00:00Z",
            "latency_ms": 9.1, "route_latency_ms": 11.0}


class _Counter:
    """A transport that counts calls and serves a queue of responses."""

    def __init__(self, *responses):
        self.calls = 0
        self._responses = list(responses) or [_closed()]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, json=body)


def _client(handler, **kw) -> DMZAgent:
    return DMZAgent(api_key=_KEY, transport=httpx.MockTransport(handler), **kw)


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #


class TestTheCacheIsOffUnlessAskedFor:
    def test_every_check_is_a_round_trip_by_default(self):
        t = _Counter()
        cx = _client(t)
        for _ in range(3):
            cx.check(subject_id="user:ws:a")
        assert t.calls == 3

    def test_a_default_client_reports_no_caching_on_its_results(self):
        cx = _client(_Counter())
        r = cx.check(subject_id="user:ws:a")
        assert (r.cached, r.cache_age_ms, r.stale) == (False, 0.0, False)

    def test_fresh_is_harmless_with_the_cache_off(self):
        t = _Counter()
        cx = _client(t)
        cx.check(subject_id="user:ws:a", fresh=True)
        assert t.calls == 1


# --------------------------------------------------------------------------- #
# Serving from the cache
# --------------------------------------------------------------------------- #


class TestServingFromTheCache:
    def test_a_second_check_inside_the_ttl_makes_no_request(self):
        t = _Counter()
        cx = _client(t, cb_cache_ttl=60)
        cx.check(subject_id="user:ws:a")
        cx.check(subject_id="user:ws:a")
        assert t.calls == 1

    def test_a_served_entry_says_so_and_carries_its_age(self):
        cx = _client(_Counter(), cb_cache_ttl=60)
        cx.check(subject_id="user:ws:a")
        time.sleep(0.01)
        second = cx.check(subject_id="user:ws:a")
        assert second.cached is True
        assert second.stale is False
        assert second.cache_age_ms >= 5, "the age has to be real, not a placeholder"

    def test_the_servers_own_numbers_are_not_rewritten(self):
        """`latency_ms` and `checked_at` describe the check that happened.
        Restating them as the cache hit would erase the only record of
        when the server was last asked."""
        cx = _client(_Counter(), cb_cache_ttl=60)
        fresh = cx.check(subject_id="user:ws:a")
        cached = cx.check(subject_id="user:ws:a")
        assert cached.latency_ms == fresh.latency_ms == 12.3
        assert cached.route_latency_ms == fresh.route_latency_ms == 18.7
        assert cached.checked_at == fresh.checked_at
        assert cached.raw == fresh.raw

    def test_the_decision_itself_survives_the_round_trip(self):
        cx = _client(_Counter(_open()), cb_cache_ttl=60)
        first = cx.check(subject_id="user:ws:a")
        second = cx.check(subject_id="user:ws:a")
        assert (second.state, second.allow, second.reason) == \
               (first.state, first.allow, first.reason)
        assert second.fired_policies == first.fired_policies

    def test_an_expired_entry_is_not_served(self):
        t = _Counter()
        cx = _client(t, cb_cache_ttl=0.02)
        cx.check(subject_id="user:ws:a")
        time.sleep(0.05)
        again = cx.check(subject_id="user:ws:a")
        assert t.calls == 2
        assert again.cached is False

    def test_fresh_bypasses_the_cache_and_replaces_it(self):
        t = _Counter(_closed(), _open())
        cx = _client(t, cb_cache_ttl=60)
        assert cx.check(subject_id="user:ws:a").allow is True
        forced = cx.check(subject_id="user:ws:a", fresh=True)
        assert t.calls == 2
        assert forced.allow is False and forced.cached is False
        assert cx.check(subject_id="user:ws:a").allow is False, "the refresh was stored"

    def test_guard_passes_fresh_through(self):
        t = _Counter()
        cx = _client(t, cb_cache_ttl=60)
        with cx.guard(subject_id="user:ws:a"):
            pass
        with cx.guard(subject_id="user:ws:a", fresh=True):
            pass
        assert t.calls == 2

    def test_guard_raises_on_a_cached_open_the_same_as_a_fresh_one(self):
        from dmzagent.errors import CBOpenError
        cx = _client(_Counter(_open()), cb_cache_ttl=60)
        cx.check(subject_id="user:ws:a")
        with pytest.raises(CBOpenError):
            with cx.guard(subject_id="user:ws:a", raise_on_open=True):
                pass


# --------------------------------------------------------------------------- #
# One TTL for every state
# --------------------------------------------------------------------------- #


class TestOneTtlForEveryState:
    @pytest.mark.parametrize("body", [_closed(), _open()], ids=["closed", "open"])
    def test_allow_and_deny_expire_together(self, body):
        """Holding a deny longer than an allow is a safety policy, and it
        belongs to whoever set the TTL."""
        t = _Counter(body)
        cx = _client(t, cb_cache_ttl=0.02)
        cx.check(subject_id="user:ws:a")
        assert cx.check(subject_id="user:ws:a").cached is True
        time.sleep(0.05)
        assert cx.check(subject_id="user:ws:a").cached is False
        assert t.calls == 2


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


class TestKeys:
    def test_subject_and_interaction_with_the_same_id_do_not_collide(self):
        t = _Counter()
        cx = _client(t, cb_cache_ttl=60)
        cx.check(subject_id="x")
        cx.check(interaction_id="x")
        assert t.calls == 2, "a subject scope and an interaction scope are different questions"

    def test_different_subjects_are_cached_separately(self):
        t = _Counter(_closed(), _open())
        cx = _client(t, cb_cache_ttl=60)
        assert cx.check(subject_id="a").allow is True
        assert cx.check(subject_id="b").allow is False
        assert cx.check(subject_id="a").allow is True, "b's deny did not overwrite a"


# --------------------------------------------------------------------------- #
# Bounded
# --------------------------------------------------------------------------- #


class TestTheCacheIsBounded:
    def test_least_recently_used_is_evicted(self):
        cache = CBStateCache(60, max_entries=2)
        r = CheckResult.from_response(_closed())
        cache.put(("subject", "a"), r)
        cache.put(("subject", "b"), r)
        cache.get(("subject", "a"))          # touch a, so b is now oldest
        cache.put(("subject", "c"), r)
        assert len(cache) == 2
        assert cache.get(("subject", "b")) is None
        assert cache.get(("subject", "a")) is not None
        assert cache.get(("subject", "c")) is not None

    def test_a_client_seeing_many_subjects_does_not_grow_without_limit(self):
        cx = _client(_Counter(), cb_cache_ttl=60, cb_cache_max_entries=8)
        for i in range(50):
            cx.check(subject_id=f"user:ws:{i}")
        assert len(cx._cb_cache) == 8

    def test_a_max_below_one_is_refused(self):
        with pytest.raises(ValueError):
            CBStateCache(60, max_entries=0)


# --------------------------------------------------------------------------- #
# Failure
# --------------------------------------------------------------------------- #


class TestWhenTheCheckFails:
    def test_raise_is_the_default_and_matches_a_client_with_no_cache(self):
        t = _Counter(_closed(), httpx.ConnectError("down"))
        cx = _client(t, cb_cache_ttl=60)
        cx.check(subject_id="user:ws:a")
        with pytest.raises(ServerError):
            cx.check(subject_id="user:ws:a", fresh=True)

    def test_last_known_serves_the_previous_state_marked_stale(self):
        t = _Counter(_open(), httpx.ConnectError("down"))
        cx = _client(t, cb_cache_ttl=60, cb_cache_on_error="last_known")
        cx.check(subject_id="user:ws:a")
        served = cx.check(subject_id="user:ws:a", fresh=True)
        assert served.cached is True and served.stale is True
        assert served.allow is False, "the last known state, not an optimistic default"

    def test_last_known_serves_an_expired_entry_too(self):
        t = _Counter(_open(), httpx.ConnectError("down"))
        cx = _client(t, cb_cache_ttl=0.02, cb_cache_on_error="last_known")
        cx.check(subject_id="user:ws:a")
        time.sleep(0.05)
        served = cx.check(subject_id="user:ws:a")
        assert served.stale is True and served.allow is False

    def test_last_known_with_nothing_known_raises(self):
        """Never an invented state for a subject this client has never
        successfully checked."""
        t = _Counter(httpx.ConnectError("down"))
        cx = _client(t, cb_cache_ttl=60, cb_cache_on_error="last_known")
        with pytest.raises(ServerError):
            cx.check(subject_id="never-seen")

    def test_a_failure_is_never_itself_cached(self):
        t = _Counter(_closed(), httpx.ConnectError("down"), _open())
        cx = _client(t, cb_cache_ttl=60, cb_cache_on_error="last_known")
        cx.check(subject_id="user:ws:a")
        cx.check(subject_id="user:ws:a", fresh=True)          # fails, serves stale
        third = cx.check(subject_id="user:ws:a", fresh=True)  # recovers
        assert third.cached is False and third.allow is False
        assert cx.check(subject_id="user:ws:a").allow is False

    def test_a_rate_limit_is_an_answer_and_is_not_masked(self):
        """429 carries a retry_after the caller can act on. Serving a
        cached state instead would drop that signal."""
        def route(request: httpx.Request) -> httpx.Response:
            if route.n == 0:
                route.n += 1
                return httpx.Response(200, json=_closed())
            return httpx.Response(429, headers={"Retry-After": "30"}, json={"detail": "slow down"})
        route.n = 0
        cx = _client(route, cb_cache_ttl=60, cb_cache_on_error="last_known")
        cx.check(subject_id="user:ws:a")
        with pytest.raises(RateLimitError) as e:
            cx.check(subject_id="user:ws:a", fresh=True)
        assert e.value.retry_after == 30


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class TestConfiguration:
    def test_an_unknown_error_policy_is_refused_by_name(self):
        with pytest.raises(ValueError, match="cb_cache_on_error"):
            _client(_Counter(), cb_cache_on_error="fail_open")

    def test_last_known_without_a_ttl_is_refused(self):
        """There is nothing to fall back TO. Accepting the pair would
        leave someone believing they had an outage story that can never
        fire."""
        with pytest.raises(ValueError, match="cb_cache_ttl"):
            _client(_Counter(), cb_cache_on_error="last_known")

    def test_a_zero_ttl_disables_the_cache_rather_than_caching_forever(self):
        t = _Counter()
        cx = _client(t, cb_cache_ttl=0)
        cx.check(subject_id="user:ws:a")
        cx.check(subject_id="user:ws:a")
        assert t.calls == 2


# --------------------------------------------------------------------------- #
# Thread safety (§4.2 — the client is shareable, so the cache must be)
# --------------------------------------------------------------------------- #


class TestThreadSafety:
    def test_concurrent_checks_on_one_client_do_not_corrupt_the_cache(self):
        cx = _client(_Counter(), cb_cache_ttl=60, cb_cache_max_entries=16)
        errors: list[BaseException] = []

        def work(n: int):
            try:
                for i in range(40):
                    cx.check(subject_id=f"user:ws:{(n + i) % 32}")
            except BaseException as e:      # noqa: BLE001 — recorded, re-raised below
                errors.append(e)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(cx._cb_cache) <= 16

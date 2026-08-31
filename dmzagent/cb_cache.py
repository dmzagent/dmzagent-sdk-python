"""In-process circuit-breaker state cache (sdk-spec.md §4.4).

`check()` is a network round trip on a path callers put in front of a
sensitive action, and it is often the only synchronous DMZAgent call in a
request. This removes that round trip for repeated checks on the same
subject.

WHAT THE CALLER IS CHOOSING. A cached `closed` is an allow the server
might no longer give. **The TTL is the maximum time a newly-opened
breaker can go unobserved by this client.** Nothing here softens that,
and every served entry carries its age so the caller can see it.

One TTL applies to every state. Holding a deny longer than an allow is a
safety policy and it belongs to whoever set the TTL, not to this module.

The clock is `time.monotonic()`: a TTL measured against the wall clock
would expire early or late whenever the host's time is adjusted, and the
adjustment is invisible to the caller.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .models import CheckResult

# The cache is off at zero. Every caller of this module treats <= 0 as
# "the caller never asked for a cache", and MUST NOT substitute a default.
DISABLED_TTL = 0.0

ON_ERROR_RAISE = "raise"
ON_ERROR_LAST_KNOWN = "last_known"
ON_ERROR_POLICIES = (ON_ERROR_RAISE, ON_ERROR_LAST_KNOWN)

DEFAULT_MAX_ENTRIES = 1024


@dataclass(frozen=True)
class _Entry:
    result: CheckResult
    stored_at: float          # time.monotonic() when it was stored


class CBStateCache:
    """Bounded, TTL'd, thread-safe map of (scope, scope_ref) → CheckResult.

    Bounded because the key is a subject id: an agent that sees a hundred
    thousand subjects would otherwise hold a hundred thousand entries for
    the life of the process. Least-recently-used goes first.
    """

    def __init__(self, ttl_s: float, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("cb_cache_max_entries must be at least 1")
        self._ttl = max(0.0, float(ttl_s))
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._ttl > DISABLED_TTL

    @property
    def ttl_s(self) -> float:
        return self._ttl

    def get(self, key: tuple[str, str]) -> tuple[CheckResult, float] | None:
        """A live entry and its age in seconds, or None.

        An expired entry is left in place rather than dropped — the
        `last_known` error policy is the reason it is still worth
        something after the TTL. Eviction is by size, never by age.
        """
        if not self.enabled:
            return None
        found = self._lookup(key)
        if found is None:
            return None
        _result, age = found
        return found if age <= self._ttl else None

    def get_any(self, key: tuple[str, str]) -> tuple[CheckResult, float] | None:
        """A live OR expired entry and its age. Only the `last_known`
        error policy may use this, and only after a failed check."""
        if not self.enabled:
            return None
        return self._lookup(key)

    def put(self, key: tuple[str, str], result: CheckResult) -> None:
        """Store a SUCCESSFUL check. Errors are never cached: a failure is
        not a state, and serving one back would turn one bad round trip
        into a TTL's worth of them."""
        if not self.enabled:
            return
        with self._lock:
            self._entries[key] = _Entry(result=result, stored_at=time.monotonic())
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _lookup(self, key: tuple[str, str]) -> tuple[CheckResult, float] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            age = time.monotonic() - entry.stored_at
        return entry.result, max(0.0, age)

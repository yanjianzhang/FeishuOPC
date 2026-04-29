"""B-1 ClaimLease unit tests — boundary semantics for TTL and
expiry. Intentionally narrow because the interesting TaskGraph
behaviour lives in ``test_task_graph.py``."""

from __future__ import annotations

import time

from feishu_agent.team.task_graph import ClaimLease


def test_acquire_sets_expires_at_from_ttl() -> None:
    before = int(time.time())
    lease = ClaimLease.acquire("trace-A", ttl_seconds=180)
    after = int(time.time())
    assert before <= lease.acquired_at <= after
    assert lease.expires_at == lease.acquired_at + 180


def test_acquire_floors_ttl_to_one_second() -> None:
    """A zero or negative TTL is clamped so we never create an
    already-expired lease by accident. Defensive: the spec's
    schema says TTL is ``int`` but the YAML parser might surface
    ``0``."""
    lease = ClaimLease.acquire("t", ttl_seconds=0)
    assert lease.expires_at == lease.acquired_at + 1
    lease_neg = ClaimLease.acquire("t", ttl_seconds=-100)
    assert lease_neg.expires_at == lease_neg.acquired_at + 1


def test_is_expired_boundary_closed() -> None:
    lease = ClaimLease(trace_id="t", acquired_at=100, expires_at=200)
    assert lease.is_expired(now=201) is True
    # exactly at expiry → counts as expired (closed right bound)
    assert lease.is_expired(now=200) is True
    assert lease.is_expired(now=199) is False


def test_from_dict_roundtrip() -> None:
    lease = ClaimLease.acquire("trace-A", 60)
    rehydrated = ClaimLease.from_dict(lease.to_dict())
    assert rehydrated == lease


def test_from_dict_handles_none_and_bad_data() -> None:
    # None and empty-dict both mean "no lease" — the sentinel YAML
    # entries ``claim: null`` and ``claim: {}`` decode identically.
    assert ClaimLease.from_dict(None) is None
    assert ClaimLease.from_dict({}) is None
    # Non-coercible types fall back to None rather than crashing
    # the whole task load — defense against hand-edited YAML.
    bad = ClaimLease.from_dict({"expires_at": "not-an-int"})
    assert bad is None

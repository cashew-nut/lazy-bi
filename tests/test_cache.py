"""app/cache.py in isolation: the generic TTL cache app/engine.py's
schema/bounds lookups sit on top of. No S3/app dependency — just the
get_or_set/clear contract itself."""
from app import cache


def setup_function(_fn):
    cache.clear()


def test_get_or_set_computes_once_then_reuses_within_ttl():
    calls = []

    def compute():
        calls.append(1)
        return "value"

    assert cache.get_or_set("k", 60.0, compute) == "value"
    assert cache.get_or_set("k", 60.0, compute) == "value"
    assert len(calls) == 1


def test_different_keys_never_collide():
    assert cache.get_or_set("a", 60.0, lambda: "A") == "A"
    assert cache.get_or_set("b", 60.0, lambda: "B") == "B"
    assert cache.get_or_set("a", 60.0, lambda: "A2") == "A"  # still cached


def test_expired_entry_is_recomputed(monkeypatch):
    calls = []

    def compute():
        calls.append(1)
        return len(calls)

    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])

    assert cache.get_or_set("k", 10.0, compute) == 1
    now[0] += 5  # inside the 10s TTL
    assert cache.get_or_set("k", 10.0, compute) == 1
    now[0] += 10  # now past it
    assert cache.get_or_set("k", 10.0, compute) == 2
    assert len(calls) == 2


def test_clear_forces_recompute():
    calls = []

    def compute():
        calls.append(1)
        return "value"

    cache.get_or_set("k", 60.0, compute)
    cache.clear()
    cache.get_or_set("k", 60.0, compute)
    assert len(calls) == 2


def test_expired_entries_are_swept_on_a_later_miss(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])

    cache.get_or_set("short-lived", 1.0, lambda: "x")
    assert "short-lived" in cache._store
    now[0] += 5  # well past that entry's 1s TTL
    cache.get_or_set("other", 60.0, lambda: "y")  # any miss sweeps expired keys
    assert "short-lived" not in cache._store

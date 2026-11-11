"""Connection rate budgets: opt-in, never bursting past the limit, never stalling other connections.

Guards against audit round 4: every connection defaulted to 120 requests/minute
(rpm_limit=0 was silently stored as 120), the synchronous limiter slept while holding
a lock shared by all connections, and the async limiter did not re-check after waiting.
"""
import asyncio
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import core.llm_gateway as gwmod


# Other suites reload core.llm_gateway, so always resolve names through the module.
def _RateBudget():
    return gwmod._RateBudget()


_REAL_ASYNC_SLEEP = asyncio.sleep


class FakeClock:
    """Deterministic time for the limiter: sleep() advances the clock."""
    def __init__(self):
        self.now = 1_000_000.0
        self.lock = threading.Lock()

    def time(self):
        with self.lock:
            return self.now

    def sleep(self, s):
        with self.lock:
            self.now += s

    async def asleep(self, s):
        self.sleep(s)
        await _REAL_ASYNC_SLEEP(0)


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(gwmod.time, "time", c.time)
    monkeypatch.setattr(gwmod.time, "sleep", c.sleep)
    monkeypatch.setattr(gwmod.asyncio, "sleep", c.asleep)
    return c


def test_zero_means_no_limit(clock):
    b = _RateBudget()
    for _ in range(1000):
        b.acquire("c", 0, 0)
    assert clock.now == 1_000_000.0  # never waited


def test_sync_rpm_never_exceeded_under_concurrency(clock):
    b = _RateBudget()
    taken = []
    lk = threading.Lock()

    def worker():
        for _ in range(5):
            b.acquire("c", 3, 0)
            with lk:
                taken.append(clock.time())

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join(timeout=10) for t in ts]
    assert len(taken) == 20
    taken.sort()
    for i, t0 in enumerate(taken):
        in_window = [t for t in taken[i:] if t < t0 + 60.0]
        assert len(in_window) <= 3


def test_async_rpm_never_exceeded_under_concurrency(clock):
    b = _RateBudget()
    taken = []

    async def one():
        await b.aacquire("c", 2, 0)
        taken.append(clock.time())

    async def main():
        await asyncio.gather(*[one() for _ in range(7)])

    asyncio.run(main())
    taken.sort()
    assert len(taken) == 7
    for i, t0 in enumerate(taken):
        assert len([t for t in taken[i:] if t < t0 + 60.0]) <= 2


def test_waiting_connection_does_not_stall_others():
    """Real time: connection A waits ~60 s for its next slot; B must not wait for it."""
    b = _RateBudget()
    b.acquire("A", 1, 0)                       # A's one slot this minute
    started = threading.Event()

    def blocked_a():
        started.set()
        b.acquire("A", 1, 0)                   # sleeps ~60 s outside the lock

    t = threading.Thread(target=blocked_a, daemon=True)
    t.start()
    started.wait(1)
    time.sleep(0.05)
    t0 = time.monotonic()
    b.acquire("B", 1, 0)
    assert time.monotonic() - t0 < 0.5


def test_daily_budget_raises_instead_of_sleeping(clock):
    b = _RateBudget()
    for _ in range(3):
        b.acquire("c", 0, 3)
    with pytest.raises(gwmod.RateBudgetExhausted, match="resume the run"):
        b.acquire("c", 0, 3)
    clock.sleep(86_400)                        # next UTC day
    b.acquire("c", 0, 3)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DB_PATH", str(tmp_path / "rb.db"))
    for m in [m for m in list(sys.modules) if m.startswith(("backend", "core", "ee", "config"))]:
        del sys.modules[m]
    import backend.deps as deps
    from backend.main import app
    c = TestClient(app)
    _, k = deps.db.create_api_key("a", role="admin")
    c.headers["X-API-Key"] = k
    return c, deps.db


def test_connection_defaults_are_unlimited_and_zero_is_honoured(client):
    c, db = client
    base = {"provider_type": "openai_compatible", "base_url": "http://127.0.0.1:8080/v1", "api_key": "k"}
    p1 = c.post("/api/providers", json={**base, "name": "default"}).json()
    p2 = c.post("/api/providers", json={**base, "name": "zero", "rpm_limit": 0, "rpd_limit": 0, "max_input_tokens": 0}).json()
    p3 = c.post("/api/providers", json={**base, "name": "limited", "rpm_limit": 30, "rpd_limit": 500, "max_input_tokens": 8000}).json()
    for pid in (p1["id"], p2["id"]):
        rec = db.get_provider_credentials(pid)
        assert (rec["rpm_limit"], rec["rpd_limit"], rec["max_input_tokens"]) == (0, 0, 0)
    rec = db.get_provider_credentials(p3["id"])
    assert (rec["rpm_limit"], rec["rpd_limit"], rec["max_input_tokens"]) == (30, 500, 8000)

"""Blocking MCP work runs one call at a time and cancellation never strands the lock."""
import asyncio
import threading
import time

import pytest

from graphify_worktree.mcp_server import Serializer


def test_calls_run_one_at_a_time_in_worker_threads():
    active, peak, threads = 0, 0, set()
    guard = threading.Lock()

    def work(cancelled):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            threads.add(threading.get_ident())
        time.sleep(0.05)
        with guard:
            active -= 1
        return "done"

    async def scenario():
        serializer = Serializer()
        return await asyncio.gather(*(serializer.run(work) for _ in range(4))), threading.get_ident()

    results, loop_thread = asyncio.run(scenario())
    assert results == ["done"] * 4
    assert peak == 1
    assert loop_thread not in threads


def test_cancelled_waiter_never_runs_and_lock_is_released():
    ran = []
    release = threading.Event()

    def slow(cancelled):
        release.wait(2)
        return "first"

    def record(cancelled):
        ran.append(True)

    async def scenario():
        serializer = Serializer()
        first = asyncio.ensure_future(serializer.run(slow))
        await asyncio.sleep(0.05)
        waiter = asyncio.ensure_future(serializer.run(record))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        assert await first == "first"
        assert await asyncio.wait_for(serializer.run(lambda cancelled: "after"), 2) == "after"

    asyncio.run(scenario())
    assert ran == []


def test_cancelling_a_running_call_returns_promptly_and_signals_the_worker():
    seen_cancel = threading.Event()
    finished = threading.Event()

    def stubborn(cancelled):
        # Simulates guarded work that checks for cancellation at its next boundary.
        cancelled.wait(2)
        seen_cancel.set()
        time.sleep(0.1)
        finished.set()

    async def scenario():
        serializer = Serializer()
        call = asyncio.ensure_future(serializer.run(stubborn))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert time.monotonic() - started < 0.1, "a cancelled request must not wait for its worker"
        # The next call waits for the abandoned worker to finish, then runs.
        assert await asyncio.wait_for(serializer.run(lambda cancelled: finished.is_set()), 2) is True

    asyncio.run(scenario())
    assert seen_cancel.is_set()

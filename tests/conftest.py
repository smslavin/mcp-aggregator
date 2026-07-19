import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import server as agg  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def _reset_aggregator_state():
    """server.py holds all state in module-level globals. Reset them around
    each test so backends registered by one test don't leak into the next.

    _shutdown_event is recreated (not just .clear()ed) every test: an
    asyncio.Event binds to whichever event loop first calls .wait() on it
    and stays bound forever. Production only ever runs one loop for the
    process's lifetime, so this never bites there — but pytest-anyio gives
    each test its own fresh loop, so a stale Event from a previous test
    raises "bound to a different event loop" the moment a later test's pool
    awaits it. Recreating it here, inside this async fixture, binds it
    fresh to the current test's loop.

    Tests that start a persistent-pool task must cancel and await it
    themselves, in their own event loop, before returning — for the same
    per-test-loop reason, cancelling a task from here would be reaching
    across loops too."""
    agg._shutdown_event = asyncio.Event()
    yield
    if agg._pool_tasks:
        leaked = list(agg._pool_tasks)
        agg._pool_tasks.clear()
        raise RuntimeError(
            f"test left pool task(s) running: {leaked} — cancel and await "
            "them in the test itself before returning"
        )
    agg._tool_registry.clear()
    agg._tool_list.clear()
    agg._backends.clear()
    agg._session_pool.clear()

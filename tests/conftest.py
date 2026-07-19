import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import server as agg  # noqa: E402


@pytest.fixture(autouse=True)
async def _reset_aggregator_state():
    """server.py holds all state in module-level globals. Reset them around
    each test so backends registered by one test don't leak into the next.

    Tests that start a persistent-pool task must cancel and await it
    themselves, in their own event loop, before returning — pytest-anyio
    runs each fixture phase in its own loop, so cancelling a task from here
    (a different loop than the one that created it) can hang indefinitely
    trying to tear down its subprocess pipes. This teardown only touches
    plain dicts, which are loop-agnostic."""
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
    agg._shutdown_event.clear()

import asyncio
import sys
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import server as agg  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def connect(config: dict, source: str = "file") -> "agg.BackendState":
    """Add and activate a backend as the backends file would, pool included."""
    state = agg._add(config, source)
    assert await agg._activate(state), state.last_error
    if state.pooled:
        with anyio.fail_after(10):
            await state.session_ready.wait()
    return state


@pytest.fixture(autouse=True)
async def _reset_aggregator_state():
    """server.py holds all state in module-level globals. Reset them around
    each test so backends registered by one test don't leak into the next.

    _shutdown_event is recreated (not just .clear()ed) every test: an
    asyncio.Event binds to whichever event loop first calls .wait() on it
    and stays bound forever. Production only ever runs one loop for the
    process's lifetime, but each test gets its own loop. Teardown removes
    every backend, which cancels its pool task, and cancels the retry and
    notification tasks, in the loop the test ran in."""
    agg._shutdown_event = asyncio.Event()
    yield
    for name in list(agg._states):
        await agg._remove(name)
    tasks = [t for t in [agg._retry_task, *agg._background] if t is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    agg._retry_task = None
    agg._background.clear()
    agg._listening_connections.clear()
    agg._tool_registry.clear()
    agg._tool_list.clear()

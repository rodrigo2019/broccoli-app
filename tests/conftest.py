"""Shared test-loop setup.

Capture teardown runs on a worker thread (see
``DesktopSessionController._stop_capture_off_loop``): ``CaptureSession.stop()``
joins two daemon workers with a one-second timeout each plus PortAudio
teardown, which inline would freeze the event loop -- the transcript socket,
the event feed and the level meter with it -- for as long as an unplugged
device takes to let go.

That makes ``await asyncio.sleep(0)`` a useless way to wait for teardown in a
test: yielding to the loop does not wait for a thread. Pinning the loop's
default executor to a single worker turns ``asyncio.to_thread`` into a real
barrier -- a job submitted afterwards cannot start until the earlier one has
finished -- which is what ``tests.fakes.settle`` uses instead of a hopeful
sleep. See ``settle``'s docstring for the other half.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def default_executor_workers() -> int:
    """How many worker threads the test loop's default executor gets.

    One, so that ``settle`` is a barrier. A test that deliberately needs two
    hops in flight at once -- a capture open parked on a thread while a stop
    lands, say -- widens it with
    ``@pytest.mark.parametrize("default_executor_workers", [N])``, which
    overrides this fixture for that test alone.
    """
    return 1


@pytest.fixture(autouse=True)
async def loop_default_executor(default_executor_workers: int) -> AsyncIterator[None]:
    executor = ThreadPoolExecutor(
        max_workers=default_executor_workers, thread_name_prefix="broccoli-test"
    )
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        yield
    finally:
        executor.shutdown(wait=True)

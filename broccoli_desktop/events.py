"""In-memory UI event fan-out without sensitive desktop payloads."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable

from broccoli_desktop.models import UiEvent

#: snapshot() has one consumer, a test. Keeping a bounded tail preserves it
#: without retaining every transcript delta for the life of the process.
EVENT_HISTORY_MAX = 256

#: The outbox bound. Generous -- a healthy socket drains within milliseconds
#: and coalescing keeps the delta share to the number of open utterances -- so
#: reaching it means the renderer has been gone for a long time, not that the
#: meeting is busy.
OUTBOX_MAX_PENDING = 1024

#: Shown once when the bound forces a final event out; the key exists in every
#: catalog and the window renders it like any other warning.
OUTBOX_OVERFLOW_MESSAGE = "notify.transcript.mayBeIncomplete"

EventSubscriber = Callable[[UiEvent], None]

logger = logging.getLogger(__name__)


class EventHub:
    """Publish immutable UI events to local consumers and retain a snapshot."""

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []
        self._events: deque[UiEvent] = deque(maxlen=EVENT_HISTORY_MAX)

    def subscribe(self, subscriber: EventSubscriber) -> None:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    def publish(self, event: UiEvent) -> None:
        if not isinstance(event, UiEvent):
            raise TypeError("EventHub accepts UiEvent values only.")
        self._events.append(event)
        for subscriber in tuple(self._subscribers):
            subscriber(event)

    def snapshot(self) -> list[UiEvent]:
        return list(self._events)


class UiEventOutbox:
    """Single-consumer outbound buffer for one ``/api/events`` socket.

    The plain ``asyncio.Queue`` this replaces was unbounded: once the window's
    renderer fell behind, ``send_json`` backpressured, the capture kept
    publishing, and every delta of the meeting was retained -- memory growth
    that also guaranteed a burst on resume, making the slow renderer slower.
    This buffer is bounded and sheds the one event class that is safe to shed.

    A delta is wholly superseded by the next delta or the segment for its
    utterance, so only the newest delta per utterance is kept -- a dict keyed
    by utterance id, which bounds the high-rate class to the number of open
    utterances by construction. Everything else queues in arrival order.

    ``get`` delivers the overflow warning first (kept as a flag, never in the
    queue, so it cannot itself rotate out), then deltas in first-queued order,
    then the rest. Draining deltas ahead of the queue is what keeps a delta
    ahead of its own utterance's segment; across utterances the reordering is
    harmless, since provisional rows are independent and status events are
    idempotent snapshots.

    ``put`` must run on the event loop -- the hub subscriber hands it over via
    ``call_soon_threadsafe`` -- and ``get`` has exactly one consumer, the
    socket's sender. ``get`` pops nothing before returning, so cancelling a
    waiting ``get`` loses no event.
    """

    def __init__(self, *, max_pending: int = OUTBOX_MAX_PENDING) -> None:
        self._deltas: dict[str, UiEvent] = {}
        self._events: deque[UiEvent] = deque()
        self._ready = asyncio.Event()
        self._max_pending = max_pending
        self._deliver_warning = False
        self._warned = False
        self.dropped_deltas = 0
        self.dropped_events = 0

    @property
    def pending(self) -> int:
        return len(self._deltas) + len(self._events) + (1 if self._deliver_warning else 0)

    def put(self, event: UiEvent) -> None:
        if event.type == "delta" and event.delta is not None:
            # Replacing at an existing key keeps its dict position, so the
            # utterance keeps its place in the delta order.
            self._deltas[event.delta.utterance_id] = event
        else:
            self._events.append(event)
        if len(self._deltas) + len(self._events) > self._max_pending:
            self._drop_one()
        self._ready.set()

    def _drop_one(self) -> None:
        if self._deltas:
            oldest = next(iter(self._deltas))
            del self._deltas[oldest]
            self.dropped_deltas += 1
            return
        self._events.popleft()
        self.dropped_events += 1
        if not self._warned:
            self._warned = True
            self._deliver_warning = True
            logger.warning("[events] An event socket outbox overflowed; oldest events were dropped")

    async def get(self) -> UiEvent:
        while True:
            if self._deliver_warning:
                self._deliver_warning = False
                return UiEvent(type="warning", message=OUTBOX_OVERFLOW_MESSAGE)
            if self._deltas:
                oldest = next(iter(self._deltas))
                return self._deltas.pop(oldest)
            if self._events:
                return self._events.popleft()
            self._ready.clear()
            await self._ready.wait()

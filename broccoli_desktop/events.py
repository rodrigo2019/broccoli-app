"""In-memory UI event fan-out without sensitive desktop payloads."""

from __future__ import annotations

from collections.abc import Callable

from broccoli_desktop.models import UiEvent

EventSubscriber = Callable[[UiEvent], None]


class EventHub:
    """Publish immutable UI events to local consumers and retain a snapshot."""

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []
        self._events: list[UiEvent] = []

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

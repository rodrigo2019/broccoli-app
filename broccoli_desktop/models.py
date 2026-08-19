"""Immutable local models shared across the desktop client."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

MAX_TITLE_LENGTH = 120


def validate_title(title: str) -> str:
    """Validate and normalize an optional session title."""
    normalized_title = title.strip()
    if len(normalized_title) > MAX_TITLE_LENGTH:
        raise ValueError(f"Titles must be at most {MAX_TITLE_LENGTH} characters.")
    return normalized_title


@dataclass(frozen=True)
class DeviceDescriptor:
    device_id: str
    label: str
    kind: Literal["mic", "system"]


@dataclass(frozen=True)
class SessionSummary:
    uuid_code: str
    title: str
    status: str
    started_at: str | None
    ended_at: str | None
    device_label: str
    segment_count: int
    is_live: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", validate_title(self.title))


@dataclass(frozen=True)
class TranscriptSegment:
    utterance_id: str
    channel: Literal["mic", "system"]
    text: str
    started_offset_ms: int
    ended_offset_ms: int


@dataclass(frozen=True)
class SessionPage:
    sessions: tuple[SessionSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SegmentPage:
    segments: tuple[TranscriptSegment, ...]
    next_cursor: str | None


class ConnectionState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"
    DEVICE_SELECTION_REQUIRED = "device_selection_required"


@dataclass(frozen=True)
class TranscriptDelta:
    channel: Literal["mic", "system"]
    utterance_id: str
    text: str
    started_offset_ms: int


@dataclass(frozen=True)
class UiEvent:
    type: str
    state: ConnectionState | None = None
    session: SessionSummary | None = None
    delta: TranscriptDelta | None = None
    segment: TranscriptSegment | None = None
    message: str | None = None

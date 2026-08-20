"""Live desktop session composition, transcript updates, and recovery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from broccoli_desktop.audio import AudioPipeline
from broccoli_desktop.capture import CaptureBackend, CaptureSession, DeviceUnavailableError
from broccoli_desktop.events import EventHub
from broccoli_desktop.models import (
    AudioFrame,
    CaptureEvent,
    ConnectionState,
    SessionSummary,
    TranscriptDelta,
    TranscriptSegment,
    UiEvent,
)
from broccoli_desktop.protocol import encode_audio_frame
from broccoli_desktop.remote import (
    CreditWarning,
    ListeningRemote,
    RemoteCreditError,
    RemoteDurationError,
    RemoteEvent,
    RemoteFailure,
    RemoteProtocolError,
    RemoteStream,
    RemoteUnauthorizedError,
    SessionEnded,
    SessionStarted,
    TranscriptDeltaEvent,
    TranscriptSegmentEvent,
)

MAX_BUFFERED_AUDIO_MS = 10_000
FRAME_DURATION_MS = 100
RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 15)
AUTO_DETECT_LANGUAGE = ""


class SleepClock(Protocol):
    async def sleep(self, delay: float) -> None: ...


@dataclass(frozen=True)
class CaptureChoices:
    microphone_id: str
    system_device_id: str


class DesktopSessionController:
    """Coordinate capture, encoded audio transport, and local transcript events."""

    def __init__(
        self,
        remote: ListeningRemote,
        capture_backend: CaptureBackend,
        *,
        clock: SleepClock | Callable[[float], Awaitable[None]] | None = None,
        on_authentication_failure: Callable[[], None] | None = None,
    ) -> None:
        self._remote = remote
        self._capture_backend = capture_backend
        self._clock = clock
        self._on_authentication_failure = on_authentication_failure
        self.events = EventHub()
        self.state = ConnectionState.IDLE
        self.pending_deltas: dict[str, TranscriptDelta] = {}
        self._buffered_frames: list[AudioFrame] = []
        self._capture: CaptureSession | None = None
        self._stream: RemoteStream | None = None
        self._recovery_stream: RemoteStream | None = None
        self._pipeline: AudioPipeline | None = None
        self._session: SessionSummary | None = None
        self._session_uuid: str | None = None
        self._choices: CaptureChoices | None = None
        self._device_label: str | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._recovery_active = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def buffered_audio_ms(self) -> int:
        return len(self._buffered_frames) * FRAME_DURATION_MS

    @property
    def selected_devices(self) -> CaptureChoices | None:
        """Return the currently selected local capture device identities."""
        return self._choices

    @property
    def session(self) -> SessionSummary | None:
        """Return the active local session summary without remote payload data."""
        return self._session

    @property
    def pipeline_base_offset_ms(self) -> int:
        if self._pipeline is None:
            return 0
        return self._pipeline_base_offset_ms

    async def start_new(self, choices: CaptureChoices, title: str) -> SessionSummary:
        """Open a new remote session with a local capture title."""
        return await self._open(choices, resume_code=None, title=title)

    async def resume(self, uuid_code: str, choices: CaptureChoices) -> SessionSummary:
        """Resume the selected remote session with a fresh local capture lifecycle."""
        return await self._open(choices, resume_code=uuid_code, title=None)

    async def stop(self) -> None:
        """Stop capture before ending the remote session and publishing stopped."""
        if self.state is ConnectionState.STOPPED:
            return
        self.stop_local_capture()
        self._clear_buffered_frames()
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        if stream is None and self.state is ConnectionState.RECONNECTING:
            stream = await self._connect_terminal_stream()
        if stream is not None:
            await self._end_and_close_stream(stream)
        self._set_state(ConnectionState.STOPPED)

    def stop_local_capture(self) -> None:
        """Stop capture synchronously while controller finalization remains pending."""
        self._stop_capture()

    def restore_selected_devices(self, choices: CaptureChoices) -> None:
        """Restore a previously validated local selection without starting capture."""
        self._choices = choices

    def clear_selected_devices(self) -> None:
        """Forget an idle local selection after the user restores default settings."""
        if self.state in {
            ConnectionState.STARTING,
            ConnectionState.STREAMING,
            ConnectionState.RECONNECTING,
        }:
            raise RuntimeError("An active capture owns the selected devices.")
        self._choices = None

    def set_authentication_failure_handler(self, callback: Callable[[], None] | None) -> None:
        """Set the narrow local notification used when background recovery loses auth."""
        self._on_authentication_failure = callback

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        """Update the active capture label without calling an unavailable remote endpoint."""
        if self._session is None or self._session_uuid != uuid_code:
            raise RemoteProtocolError("The requested local session is not active.")
        summary = replace(self._session, title=title)
        self._session = summary
        self.events.publish(UiEvent(type="session", session=summary))
        return summary

    def enqueue_audio_frames(self, frames: Iterable[AudioFrame]) -> None:
        """Keep a bounded, offset-ordered reconnect buffer of encoded frames."""
        self._buffered_frames.extend(frames)
        self._buffered_frames.sort(key=lambda frame: frame.offset_ms)
        excess = len(self._buffered_frames) - MAX_BUFFERED_AUDIO_MS // FRAME_DURATION_MS
        if excess > 0:
            del self._buffered_frames[:excess]

    async def _open(
        self, choices: CaptureChoices, *, resume_code: str | None, title: str | None
    ) -> SessionSummary:
        if self.state in {
            ConnectionState.STARTING,
            ConnectionState.STREAMING,
            ConnectionState.RECONNECTING,
        }:
            raise RuntimeError("A desktop session is already active.")
        previous_session = (
            self._session if resume_code and self._session_uuid == resume_code else None
        )
        previous_offset_ms = (
            self._pipeline.next_offset_ms if previous_session and self._pipeline else 0
        )
        self._clear_buffered_frames()
        self.pending_deltas.clear()
        self._loop = asyncio.get_running_loop()
        self._choices = choices
        self._device_label = self._selected_system_label(choices)
        self._set_state(ConnectionState.STARTING)
        stream = None
        remote_period_started = False
        try:
            stream = await self._remote.connect_stream(
                resume_code=resume_code,
                device_label=self._device_label,
                language=AUTO_DETECT_LANGUAGE,
            )
            iterator = stream.events()
            started = await anext(iterator)
            if not isinstance(started, SessionStarted):
                raise RemoteProtocolError("Remote stream did not start a session.")
            remote_period_started = True
            if resume_code is not None and started.uuid_code != resume_code:
                raise RemoteProtocolError("Remote resumed an unexpected session.")
            self._set_pipeline(previous_offset_ms)
            summary = SessionSummary(
                uuid_code=started.uuid_code,
                title=title
                if title is not None
                else (previous_session.title if previous_session else ""),
                status="live",
                started_at=previous_session.started_at if previous_session else None,
                ended_at=None,
                device_label=self._device_label,
                segment_count=previous_session.segment_count if previous_session else 0,
                is_live=True,
            )
            self._session = summary
            self._session_uuid = started.uuid_code
            self._stream = stream
            self._capture = CaptureSession(
                self._capture_backend,
                choices.microphone_id,
                choices.system_device_id,
                self._on_pcm,
                on_event=self._on_capture_event,
            )
            self._capture.start()
        except DeviceUnavailableError:
            await self._close_open_stream(stream, remote_period_started)
            self._stop_capture()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.DEVICE_SELECTION_REQUIRED)
            raise
        except RemoteUnauthorizedError:
            await self._close_open_stream(stream, remote_period_started)
            self._stop_capture()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.FAILED, message="Authentication failed.")
            raise
        except Exception:
            await self._close_open_stream(stream, remote_period_started)
            self._stop_capture()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.FAILED, message="Unable to start the session.")
            raise
        self.events.publish(UiEvent(type="session", session=summary))
        self._set_state(ConnectionState.STREAMING, session=summary)
        self._reader_task = asyncio.create_task(self._listen(iterator))
        return summary

    async def _listen(self, iterator: AsyncIterator[RemoteEvent]) -> None:
        try:
            async for event in iterator:
                await self._handle_remote_event(event)
                if isinstance(event, RemoteFailure) or self.state not in {
                    ConnectionState.STREAMING,
                    ConnectionState.RECONNECTING,
                }:
                    return
        except asyncio.CancelledError:
            return
        except RemoteUnauthorizedError:
            await self._fail_from_remote("Authentication failed.")
            self._notify_authentication_failure()
        except RemoteCreditError:
            await self._fail_from_remote("Session credits are unavailable.")
        except RemoteDurationError:
            await self._fail_from_remote("Session reached its maximum duration.")
        except Exception:
            if self.state is ConnectionState.STREAMING:
                await self._recover()

    async def _handle_remote_event(self, event: RemoteEvent) -> None:
        if isinstance(event, TranscriptDeltaEvent):
            delta = TranscriptDelta(
                channel=self._channel(event.channel),
                utterance_id=event.utterance_id,
                text=event.text,
                started_offset_ms=event.started_offset_ms,
            )
            self.pending_deltas[delta.utterance_id] = delta
            self.events.publish(UiEvent(type="delta", delta=delta))
            return
        if isinstance(event, TranscriptSegmentEvent):
            segment = TranscriptSegment(
                utterance_id=event.utterance_id,
                channel=self._channel(event.channel),
                text=event.text,
                started_offset_ms=event.started_offset_ms,
                ended_offset_ms=event.ended_offset_ms,
            )
            self.pending_deltas.pop(segment.utterance_id, None)
            if self._session is not None:
                self._session = replace(
                    self._session, segment_count=self._session.segment_count + 1
                )
            self.events.publish(UiEvent(type="segment", segment=segment))
            return
        if isinstance(event, CreditWarning):
            self.events.publish(UiEvent(type="warning", message="Session credits are running low."))
            return
        if isinstance(event, SessionEnded):
            await self._end_from_remote()
            return
        if isinstance(event, RemoteFailure):
            await self._fail_from_remote()

    async def _recover(self) -> None:
        if self._recovery_active or self._session_uuid is None or self._choices is None:
            return
        self._recovery_active = True
        failed_stream = self._stream
        self._stream = None
        self._recovery_stream = failed_stream
        self._set_state(
            ConnectionState.RECONNECTING, message="Connection interrupted. Reconnecting."
        )
        try:
            for delay in RETRY_DELAYS_SECONDS:
                await self._sleep(delay)
                if self.state is not ConnectionState.RECONNECTING:
                    return
                stream: RemoteStream | None = None
                try:
                    stream = await self._remote.connect_stream(
                        resume_code=self._session_uuid,
                        device_label=self._device_label or "",
                        language=AUTO_DETECT_LANGUAGE,
                    )
                    previous_stream = self._recovery_stream
                    self._recovery_stream = stream
                    if previous_stream is not None:
                        await self._close_stream(previous_stream)
                    iterator = stream.events()
                    started = await anext(iterator)
                    if not isinstance(started, SessionStarted):
                        raise RemoteProtocolError("Remote stream did not start a session.")
                    if started.uuid_code != self._session_uuid:
                        raise RemoteProtocolError("Remote resumed an unexpected session.")
                    self._stream = stream
                    self._recovery_stream = None
                    for frame in self._buffered_frames:
                        await stream.send_bytes(
                            encode_audio_frame(
                                channel=frame.channel, offset_ms=frame.offset_ms, pcm=frame.pcm
                            )
                        )
                    self._buffered_frames.clear()
                    self._set_state(ConnectionState.STREAMING, session=self._session)
                    self._reader_task = asyncio.create_task(self._listen(iterator))
                    return
                except RemoteUnauthorizedError:
                    await self._discard_recovery_stream(stream or self._recovery_stream)
                    self._stop_capture()
                    self._clear_buffered_frames()
                    self._notify_authentication_failure()
                    self._set_state(ConnectionState.FAILED, message="Authentication failed.")
                    return
                except RemoteCreditError:
                    await self._discard_recovery_stream(stream or self._recovery_stream)
                    await self._fail_from_remote("Session credits are unavailable.")
                    return
                except RemoteDurationError:
                    await self._discard_recovery_stream(stream or self._recovery_stream)
                    await self._fail_from_remote("Session reached its maximum duration.")
                    return
                except Exception:
                    await self._discard_recovery_stream(stream)
                    self.events.publish(
                        UiEvent(type="recoverable_error", message="Connection retry failed.")
                    )
            await self._discard_recovery_stream(self._recovery_stream)
            self._stop_capture()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.FAILED, message="Connection could not be restored.")
        finally:
            self._recovery_active = False

    async def _end_from_remote(self) -> None:
        self._stop_capture()
        self._clear_buffered_frames()
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        if stream is not None:
            await self._close_stream(stream)
        self._set_state(ConnectionState.STOPPED)

    async def _fail_from_remote(
        self, message: str = "The remote session could not continue."
    ) -> None:
        self._stop_capture()
        self._clear_buffered_frames()
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        if stream is not None:
            await self._close_stream(stream)
        self._set_state(ConnectionState.FAILED, message=message)

    def _on_pcm(self, channel: Literal["mic", "system"], pcm: bytes) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        for frame in pipeline.feed(channel, pcm):
            self._schedule_forward(frame)

    def _schedule_forward(self, frame: AudioFrame) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._forward_frame(frame)))

    async def _forward_frame(self, frame: AudioFrame) -> None:
        stream = self._stream or self._recovery_stream
        if self.state is ConnectionState.RECONNECTING:
            self.enqueue_audio_frames([frame])
            return
        if self.state is not ConnectionState.STREAMING or stream is None:
            return
        try:
            await stream.send_bytes(
                encode_audio_frame(channel=frame.channel, offset_ms=frame.offset_ms, pcm=frame.pcm)
            )
        except Exception:
            self.enqueue_audio_frames([frame])
            await self._recover()

    def _on_capture_event(self, event: CaptureEvent) -> None:
        if event.type == "device_lost":
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._handle_device_loss()))

    async def _handle_device_loss(self) -> None:
        if self.state not in {
            ConnectionState.STARTING,
            ConnectionState.STREAMING,
            ConnectionState.RECONNECTING,
        }:
            return
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        self._stop_capture()
        self._clear_buffered_frames()
        if stream is not None:
            await self._end_and_close_stream(stream)
        self._set_state(
            ConnectionState.DEVICE_SELECTION_REQUIRED,
            message="Select a replacement capture device.",
        )

    def _stop_capture(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            capture.stop()

    def _set_pipeline(self, base_offset_ms: int) -> None:
        self._pipeline = AudioPipeline(base_offset_ms=base_offset_ms)
        self._pipeline_base_offset_ms = base_offset_ms

    def _selected_system_label(self, choices: CaptureChoices) -> str:
        for device in self._capture_backend.list_devices():
            if device.device_id == choices.system_device_id:
                return device.label
        return choices.system_device_id

    def _set_state(
        self,
        state: ConnectionState,
        *,
        session: SessionSummary | None = None,
        message: str | None = None,
    ) -> None:
        self.state = state
        self.events.publish(UiEvent(type="status", state=state, session=session, message=message))

    async def _sleep(self, delay: float) -> None:
        if self._clock is None:
            await asyncio.sleep(delay)
        elif callable(self._clock):
            await self._clock(delay)
        else:
            await self._clock.sleep(delay)

    @staticmethod
    async def _close_stream(stream: object) -> None:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    async def _close_open_stream(
        self, stream: RemoteStream | None, remote_period_started: bool
    ) -> None:
        if stream is None:
            return
        if remote_period_started:
            await self._end_and_close_stream(stream)
            return
        await self._close_stream(stream)

    async def _end_and_close_stream(self, stream: RemoteStream) -> None:
        """Best-effort remote finalization whose failures cannot mask the local cause."""
        try:
            await stream.send_control({"type": "session.end"})
        except Exception:
            pass
        await self._close_stream(stream)

    def _clear_buffered_frames(self) -> None:
        self._buffered_frames.clear()

    def _notify_authentication_failure(self) -> None:
        callback = self._on_authentication_failure
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    async def _discard_recovery_stream(self, stream: RemoteStream | None) -> None:
        if stream is None:
            return
        if self._stream is stream:
            self._stream = None
        if self._recovery_stream is stream:
            self._recovery_stream = None
        await self._close_stream(stream)

    async def _connect_terminal_stream(self) -> RemoteStream | None:
        if self._session_uuid is None:
            return None
        try:
            return await self._remote.connect_stream(
                resume_code=self._session_uuid,
                device_label=self._device_label or "",
                language=AUTO_DETECT_LANGUAGE,
            )
        except Exception:
            return None

    @staticmethod
    def _channel(channel: str) -> Literal["mic", "system"]:
        if channel not in {"mic", "system"}:
            raise RemoteProtocolError("Remote transcript channel was invalid.")
        return channel

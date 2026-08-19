"""Live desktop session composition, transcript updates, and recovery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
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
    ) -> None:
        self._remote = remote
        self._capture_backend = capture_backend
        self._clock = clock
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
    def pipeline_base_offset_ms(self) -> int:
        if self._pipeline is None:
            return 0
        return self._pipeline_base_offset_ms

    async def start_new(self, choices: CaptureChoices, title: str) -> SessionSummary:
        """Open a new remote session and update its title after session.started."""
        return await self._open(choices, resume_code=None, title=title)

    async def resume(self, uuid_code: str, choices: CaptureChoices) -> SessionSummary:
        """Resume the selected remote session with a fresh local capture lifecycle."""
        return await self._open(choices, resume_code=uuid_code, title=None)

    async def stop(self) -> None:
        """Stop capture before ending the remote session and publishing stopped."""
        if self.state is ConnectionState.STOPPED:
            return
        self._stop_capture()
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        if stream is not None:
            try:
                await stream.send_control({"type": "session.end"})
            except Exception:
                pass
            try:
                await stream.close()
            except Exception:
                pass
        self._set_state(ConnectionState.STOPPED)

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        """Persist a user-selected title through the typed remote boundary."""
        summary = await self._remote.update_title(uuid_code, title)
        if self._session_uuid == uuid_code:
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
        self._loop = asyncio.get_running_loop()
        self._choices = choices
        self._device_label = self._selected_system_label(choices)
        self._set_state(ConnectionState.STARTING)
        stream = None
        try:
            stream = await self._remote.connect_stream(
                resume_code=resume_code, device_label=self._device_label
            )
            iterator = stream.events()
            started = await anext(iterator)
            if not isinstance(started, SessionStarted):
                raise RemoteProtocolError("Remote stream did not start a session.")
            if resume_code is not None and started.uuid_code != resume_code:
                raise RemoteProtocolError("Remote resumed an unexpected session.")
            self._set_pipeline(started.next_offset_ms)
            summary = await self._remote.get_session(started.uuid_code)
            if title is not None:
                summary = await self._remote.update_title(started.uuid_code, title)
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
            if stream is not None:
                await self._close_stream(stream)
            self._stop_capture()
            self._set_state(ConnectionState.DEVICE_SELECTION_REQUIRED)
            raise
        except RemoteUnauthorizedError:
            if stream is not None:
                await self._close_stream(stream)
            self._stop_capture()
            self._set_state(ConnectionState.FAILED, message="Authentication failed.")
            raise
        except Exception:
            if stream is not None:
                await self._close_stream(stream)
            self._stop_capture()
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
                    self._set_pipeline(started.next_offset_ms)
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
                    self._set_state(ConnectionState.FAILED, message="Authentication failed.")
                    return
                except Exception:
                    await self._discard_recovery_stream(stream)
                    self.events.publish(
                        UiEvent(type="recoverable_error", message="Connection retry failed.")
                    )
            await self._discard_recovery_stream(self._recovery_stream)
            self._stop_capture()
            self._set_state(ConnectionState.FAILED, message="Connection could not be restored.")
        finally:
            self._recovery_active = False

    async def _end_from_remote(self) -> None:
        self._stop_capture()
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        if stream is not None:
            await self._close_stream(stream)
        self._set_state(ConnectionState.STOPPED)

    async def _fail_from_remote(self) -> None:
        self._stop_capture()
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        if stream is not None:
            await self._close_stream(stream)
        self._set_state(ConnectionState.FAILED, message="The remote session could not continue.")

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
        if self.state is not ConnectionState.STREAMING or stream is None:
            self.enqueue_audio_frames([frame])
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
        stream = self._stream
        self._stream = None
        self._recovery_stream = None
        if stream is not None:
            await self._close_stream(stream)
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

    async def _discard_recovery_stream(self, stream: RemoteStream | None) -> None:
        if stream is None:
            return
        if self._stream is stream:
            self._stream = None
        if self._recovery_stream is stream:
            self._recovery_stream = None
        await self._close_stream(stream)

    @staticmethod
    def _channel(channel: str) -> Literal["mic", "system"]:
        if channel not in {"mic", "system"}:
            raise RemoteProtocolError("Remote transcript channel was invalid.")
        return channel

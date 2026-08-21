"""Selected microphone and WASAPI-loopback capture without audio persistence."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import Literal, Protocol

import numpy
import pyaudiowpatch

from broccoli_desktop.models import CaptureEvent, DeviceDescriptor

SAMPLE_RATE = 48_000
BLOCK_MS = 20
BLOCK_FRAMES = SAMPLE_RATE * BLOCK_MS // 1_000
BLOCK_BYTES = BLOCK_FRAMES * 2
QUEUE_BLOCKS = 50

SourcePcmCallback = Callable[[bytes], None]
PcmCallback = Callable[[Literal["mic", "system"], bytes], None]
CaptureEventCallback = Callable[[CaptureEvent], None]
CaptureErrorCallback = Callable[[Exception], None]


class DeviceUnavailableError(RuntimeError):
    """A selected capture device cannot be opened or was removed."""

    def __init__(self, display_name: str) -> None:
        super().__init__(f"The selected device '{display_name}' is unavailable.")
        self.display_name = display_name


class CaptureHandle(Protocol):
    """An active source that owns its transient queue and stream."""

    def set_error_handler(self, on_error: CaptureErrorCallback) -> None: ...

    def drain(self) -> None: ...

    def close(self) -> None: ...


class CaptureBackend(Protocol):
    """Hardware boundary used by the capture lifecycle and offline fakes."""

    def list_devices(self) -> list[DeviceDescriptor]: ...

    def open_microphone(self, device_id: str, on_pcm: SourcePcmCallback) -> CaptureHandle: ...

    def open_loopback(self, device_id: str, on_pcm: SourcePcmCallback) -> CaptureHandle: ...


class _QueuedCaptureHandle:
    """Keep PortAudio callbacks bounded and independent from PCM consumers."""

    _STOP = object()

    def __init__(self, on_pcm: SourcePcmCallback) -> None:
        self._on_pcm = on_pcm
        self._queue: Queue[bytes | Exception | object] = Queue(maxsize=QUEUE_BLOCKS)
        self._stream: object | None = None
        self._closed = Event()
        self._error_handler: CaptureErrorCallback | None = None
        self._pending_error: Exception | None = None
        self._lock = RLock()
        self._worker = Thread(target=self._run, name="broccoli-capture", daemon=True)
        self._worker.start()

    def set_stream(self, stream: object) -> None:
        self._stream = stream

    def set_error_handler(self, on_error: CaptureErrorCallback) -> None:
        with self._lock:
            self._error_handler = on_error
            pending_error = self._pending_error
            self._pending_error = None
        if pending_error is not None:
            on_error(pending_error)

    def callback(
        self, pcm: bytes, _frame_count: int, _time_info: object, status_flags: int
    ) -> tuple[None, int]:
        """PortAudio callback: enqueue only, never call consumer or wait."""
        if self._closed.is_set():
            return None, pyaudiowpatch.paAbort
        if status_flags:
            self._offer(DeviceUnavailableError("capture device"))
            return None, pyaudiowpatch.paAbort
        if len(pcm) == BLOCK_BYTES:
            self._offer(pcm)
        return None, pyaudiowpatch.paContinue

    def drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                return

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        stream = self._stream
        if stream is not None:
            stop_stream = getattr(stream, "stop_stream", None)
            if stop_stream is not None:
                stop_stream()
            close_stream = getattr(stream, "close", None)
            if close_stream is not None:
                close_stream()
        self.drain()
        self._offer(self._STOP)
        if self._worker is not current_thread():
            self._worker.join(timeout=1)

    def _offer(self, item: bytes | Exception | object) -> None:
        try:
            self._queue.put_nowait(item)
        except Full:
            if isinstance(item, bytes):
                return
            try:
                self._queue.get_nowait()
            except Empty:
                return
            try:
                self._queue.put_nowait(item)
            except Full:
                return

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                return
            if isinstance(item, Exception):
                self._report_error(item)
                return
            try:
                self._on_pcm(item)
            except Exception as error:
                self._report_error(error)
                return

    def _report_error(self, error: Exception) -> None:
        with self._lock:
            on_error = self._error_handler
            if on_error is None:
                self._pending_error = error
                return
        on_error(error)


class PyAudioCaptureBackend:
    """PyAudioWPatch adapter for direct mic and WASAPI output-loopback sources."""

    def __init__(self, pyaudio: object) -> None:
        self._pyaudio = pyaudio

    def list_devices(self) -> list[DeviceDescriptor]:
        devices = [
            DeviceDescriptor(self._device_id("mic", info), str(info["name"]), "mic")
            for info in self._microphone_infos()
        ]
        devices.extend(
            DeviceDescriptor(self._device_id("system", info), str(info["name"]), "system")
            for info in self._output_infos()
        )
        return devices

    def open_microphone(self, device_id: str, on_pcm: SourcePcmCallback) -> CaptureHandle:
        info = self._find_info("mic", device_id)
        return self._open_input(int(info["index"]), on_pcm)

    def open_loopback(self, device_id: str, on_pcm: SourcePcmCallback) -> CaptureHandle:
        output = self._find_info("system", device_id)
        try:
            loopback = self._pyaudio.get_wasapi_loopback_analogue_by_index(int(output["index"]))
        except (LookupError, OSError) as error:
            raise DeviceUnavailableError(str(output["name"])) from error
        return self._open_input(int(loopback["index"]), on_pcm)

    def _open_input(self, device_index: int, on_pcm: SourcePcmCallback) -> CaptureHandle:
        handle = _QueuedCaptureHandle(on_pcm)
        try:
            stream = self._pyaudio.open(
                format=pyaudiowpatch.paInt16,
                channels=1,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=BLOCK_FRAMES,
                stream_callback=handle.callback,
            )
        except OSError:
            handle.close()
            raise
        handle.set_stream(stream)
        return handle

    def _find_info(self, kind: str, device_id: str) -> dict[str, object]:
        infos = self._microphone_infos() if kind == "mic" else self._output_infos()
        for info in infos:
            if self._device_id(kind, info) == device_id:
                return info
        raise DeviceUnavailableError(device_id)

    def _microphone_infos(self) -> Iterator[dict[str, object]]:
        for info in self._pyaudio.get_device_info_generator():
            if int(info["maxInputChannels"]) > 0 and not bool(info.get("isLoopbackDevice")):
                yield info

    def _output_infos(self) -> Iterator[dict[str, object]]:
        for info in self._pyaudio.get_device_info_generator():
            if int(info["maxOutputChannels"]) > 0 and self._is_wasapi(info):
                yield info

    def _is_wasapi(self, info: dict[str, object]) -> bool:
        get_host_info = getattr(self._pyaudio, "get_host_api_info_by_index", None)
        if get_host_info is None:
            return True
        host_info = get_host_info(int(info["hostApi"]))
        return int(host_info["type"]) == pyaudiowpatch.paWASAPI

    @staticmethod
    def _device_id(kind: str, info: dict[str, object]) -> str:
        """Use an opaque identity that distinguishes same-named endpoints."""
        identity = f"{kind}\0{info.get('hostApi', '')}\0{info['name']}\0{info['index']}"
        return f"{kind}:{sha256(identity.encode()).hexdigest()[:24]}"


@dataclass(frozen=True)
class AudioLevelSnapshot:
    """Transient normalized levels for the local device-check UI."""

    microphone: float
    microphone_peak: float
    system: float
    system_peak: float
    active: bool


class AudioLevelMonitor:
    """Publish transient local signal levels for the settings check and capture UI.

    The monitor never retains PCM. It exposes normalized current and peak values
    so the settings UI can verify devices and the capture footer can render the
    live microphone and system excitation.
    """

    _LEVEL_DECAY_PER_SECOND = 4.0
    _PEAK_DECAY_PER_SECOND = 1.25
    _PUBLISH_INTERVAL_SECONDS = 0.05

    def __init__(self, backend: CaptureBackend) -> None:
        self._backend = backend
        self._lock = RLock()
        self._handles: dict[Literal["mic", "system"], CaptureHandle] = {}
        self._levels: dict[Literal["mic", "system"], float] = {"mic": 0.0, "system": 0.0}
        self._peaks: dict[Literal["mic", "system"], float] = {"mic": 0.0, "system": 0.0}
        self._updated_at: dict[Literal["mic", "system"], float] = {
            "mic": monotonic(),
            "system": monotonic(),
        }
        self._selection: tuple[str, str] | None = None
        self._capture_active = False
        self._last_published_at = 0.0
        self._listeners: set[Callable[[AudioLevelSnapshot], None]] = set()
        self._starting = False
        self._startup_error: Exception | None = None

    def start(self, microphone_id: str, system_device_id: str) -> None:
        """Open the selected sources, replacing any prior device check."""
        selection = (microphone_id, system_device_id)
        with self._lock:
            if self._selection == selection and self._handles:
                return
        self.stop()

        opened: dict[Literal["mic", "system"], CaptureHandle] = {}
        with self._lock:
            self._starting = True
            self._startup_error = None
        try:
            microphone = self._backend.open_microphone(
                microphone_id, lambda pcm: self._record_level("mic", pcm)
            )
            microphone.set_error_handler(self._handle_source_error)
            opened["mic"] = microphone
            system = self._backend.open_loopback(
                system_device_id, lambda pcm: self._record_level("system", pcm)
            )
            system.set_error_handler(self._handle_source_error)
            opened["system"] = system
            with self._lock:
                startup_error = self._startup_error
                if startup_error is None:
                    self._handles = opened
                    self._selection = selection
                    now = monotonic()
                    self._levels = {"mic": 0.0, "system": 0.0}
                    self._peaks = {"mic": 0.0, "system": 0.0}
                    self._updated_at = {"mic": now, "system": now}
        except OSError as error:
            self._close_handles(opened.values())
            raise DeviceUnavailableError("capture device") from error
        except Exception:
            self._close_handles(opened.values())
            raise
        finally:
            with self._lock:
                self._starting = False

        if startup_error is not None:
            self._close_handles(opened.values())
            if isinstance(startup_error, DeviceUnavailableError):
                raise startup_error
            raise DeviceUnavailableError("capture device") from startup_error
        with self._lock:
            snapshot = self._snapshot_locked(monotonic())
        self._publish(snapshot)

    def stop(self) -> None:
        """Release both temporary capture sources and reset the visible levels."""
        with self._lock:
            handles = tuple(self._handles.values())
            self._handles.clear()
            self._selection = None
            self._capture_active = False
            self._levels = {"mic": 0.0, "system": 0.0}
            self._peaks = {"mic": 0.0, "system": 0.0}
            now = monotonic()
            self._updated_at = {"mic": now, "system": now}
            self._last_published_at = 0.0
            snapshot = self._snapshot_locked(now)
        self._close_handles(handles)
        self._publish(snapshot)

    @property
    def capture_active(self) -> bool:
        """Whether the transcription capture, not a device check, owns the levels."""
        with self._lock:
            return self._capture_active

    def set_capture_active(self, active: bool) -> None:
        """Mark the shared capture session as the source of level events."""
        with self._lock:
            if active and not self._capture_active:
                now = monotonic()
                self._levels = {"mic": 0.0, "system": 0.0}
                self._peaks = {"mic": 0.0, "system": 0.0}
                self._updated_at = {"mic": now, "system": now}
            self._capture_active = bool(active)
            snapshot = self._snapshot_locked(monotonic())
        self._publish(snapshot)

    def subscribe(self, listener: Callable[[AudioLevelSnapshot], None]) -> None:
        """Subscribe to coalesced level snapshots for the local SSE endpoint."""
        with self._lock:
            self._listeners.add(listener)

    def unsubscribe(self, listener: Callable[[AudioLevelSnapshot], None]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def snapshot(self) -> AudioLevelSnapshot:
        """Return decayed display values without exposing any captured samples."""
        with self._lock:
            return self._snapshot_locked(monotonic())

    def _snapshot_locked(self, now: float, *, grace: bool = True) -> AudioLevelSnapshot:
        microphone, microphone_peak = self._decayed_values("mic", now, grace=grace)
        system, system_peak = self._decayed_values("system", now, grace=grace)
        return AudioLevelSnapshot(
            microphone=microphone,
            microphone_peak=microphone_peak,
            system=system,
            system_peak=system_peak,
            active=bool(self._handles) or self._capture_active,
        )

    def _record_level(self, channel: Literal["mic", "system"], pcm: bytes) -> None:
        level = _pcm_level(pcm)
        snapshot: AudioLevelSnapshot | None = None
        with self._lock:
            now = monotonic()
            current, peak = self._decayed_values(channel, now)
            self._levels[channel] = max(level, current)
            self._peaks[channel] = max(level, peak)
            self._updated_at[channel] = now
            if now - self._last_published_at >= self._PUBLISH_INTERVAL_SECONDS:
                self._last_published_at = now
                snapshot = self._snapshot_locked(now, grace=False)
        if snapshot is not None:
            self._publish(snapshot)

    def record(self, channel: Literal["mic", "system"], pcm: bytes) -> None:
        """Record one transient block delivered by the active transcription capture."""
        self._record_level(channel, pcm)

    def _publish(self, snapshot: AudioLevelSnapshot) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                continue

    def _decayed_values(
        self, channel: Literal["mic", "system"], now: float, *, grace: bool = False
    ) -> tuple[float, float]:
        elapsed = max(0.0, now - self._updated_at[channel])
        if grace:
            # Keep one coalescing window stable so an SSE/API read immediately
            # after a PCM block reports the received excitation at full strength.
            elapsed = max(0.0, elapsed - self._PUBLISH_INTERVAL_SECONDS)
        level = self._levels[channel] * max(0.0, 1.0 - elapsed * self._LEVEL_DECAY_PER_SECOND)
        peak = self._peaks[channel] * max(0.0, 1.0 - elapsed * self._PEAK_DECAY_PER_SECOND)
        return level, peak

    def _handle_source_error(self, error: Exception) -> None:
        with self._lock:
            if self._starting:
                self._startup_error = error
                return
        self.stop()

    @staticmethod
    def _close_handles(handles: Iterable[CaptureHandle]) -> None:
        for handle in tuple(handles):
            handle.drain()
            handle.close()


def _pcm_level(pcm: bytes) -> float:
    """Compute an RMS amplitude from PCM16 without retaining the sample data.

    numpy is already in the process via soxr; the pure-Python loop this
    replaces ran over ~96,000 samples/second across both channels. ``count``
    truncates a trailing odd byte the same way the old slice did, and the
    float32 cast happens before squaring so 16-bit samples never wrap the way
    they would if squared in an integer dtype.
    """
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return 0.0
    samples = numpy.frombuffer(pcm, dtype="<i2", count=sample_count).astype(numpy.float32)
    return float(min(1.0, numpy.sqrt(numpy.mean(numpy.square(samples))) / 32_768))


class CaptureSession:
    """Open two selected sources together and close both on every failure path."""

    def __init__(
        self,
        backend: CaptureBackend,
        microphone_id: str,
        system_device_id: str,
        on_pcm: PcmCallback,
        *,
        on_event: CaptureEventCallback | None = None,
        on_audio_level: PcmCallback | None = None,
        on_capture_state: Callable[[bool], None] | None = None,
        device_labels: Mapping[str, str] | None = None,
    ) -> None:
        """``device_labels`` is an already-taken enumeration, device id to label.

        These labels only ever name a device in a failure message, and this
        class used to buy them with two full Windows enumerations per session
        start -- one here, on the event loop, and one more in start(). The
        caller has almost always just enumerated for its own validation, and its
        copy comes off a TTL cache (Services.list_devices), so handing that over
        removes both. Left out, the old self-service behaviour is kept, which is
        what direct users of this class in the tests rely on.
        """
        self._backend = backend
        self._microphone_id = microphone_id
        self._system_device_id = system_device_id
        self._on_pcm = on_pcm
        self._on_event = on_event
        self._on_audio_level = on_audio_level
        self._on_capture_state = on_capture_state
        self._device_labels = None if device_labels is None else dict(device_labels)
        selected_ids = {microphone_id, system_device_id}
        self._selected_labels = {
            device_id: label
            for device_id, label in self._known_device_labels().items()
            if device_id in selected_ids
        }
        self._lock = RLock()
        self._handles: dict[str, CaptureHandle] = {}
        self._capture_active = False
        self._starting = False
        self._startup_error: Exception | None = None
        self._selection_invalid = False

    def _known_device_labels(self) -> Mapping[str, str]:
        """Device id to label, from the caller's enumeration or a fresh one."""
        if self._device_labels is not None:
            return self._device_labels
        return {device.device_id: device.label for device in self._backend.list_devices()}

    def start(self) -> None:
        """Start both sources or leave no active source behind."""
        with self._lock:
            if self._selection_invalid:
                raise DeviceUnavailableError("Select a replacement device")
            if self._handles:
                return
            for device_id, label in self._known_device_labels().items():
                self._selected_labels.setdefault(device_id, label)
            opening_device_id = self._microphone_id
            self._starting = True
            self._startup_error = None
            try:
                microphone = self._backend.open_microphone(
                    self._microphone_id, lambda pcm: self._on_source_pcm("mic", pcm)
                )
                self._install_handle(self._microphone_id, microphone)
                self._raise_startup_error()
                opening_device_id = self._system_device_id
                loopback = self._backend.open_loopback(
                    self._system_device_id, lambda pcm: self._on_source_pcm("system", pcm)
                )
                self._install_handle(self._system_device_id, loopback)
                self._raise_startup_error()
                self._capture_active = True
                if self._on_capture_state is not None:
                    self._on_capture_state(True)
            except DeviceUnavailableError as error:
                self._selection_invalid = True
                self.stop()
                raise DeviceUnavailableError(
                    self._selected_labels.get(opening_device_id, error.display_name)
                ) from error
            except OSError as error:
                self._selection_invalid = True
                failed_label = self._selected_labels.get(opening_device_id, opening_device_id)
                self.stop()
                raise DeviceUnavailableError(failed_label) from error
            except Exception:
                self.stop()
                raise
            finally:
                self._starting = False

    def stop(self) -> None:
        """Drain and close every source; repeated calls are harmless."""
        with self._lock:
            handles = tuple(self._handles.values())
            self._handles.clear()
            was_active = self._capture_active
            self._capture_active = False
        if was_active and self._on_capture_state is not None:
            self._on_capture_state(False)
        for handle in reversed(handles):
            handle.drain()
            handle.close()

    def _on_source_pcm(self, channel: Literal["mic", "system"], pcm: bytes) -> None:
        if self._on_audio_level is not None:
            self._on_audio_level(channel, pcm)
        self._on_pcm(channel, pcm)

    def _install_handle(self, device_id: str, handle: CaptureHandle) -> None:
        self._handles[device_id] = handle
        handle.set_error_handler(lambda error: self._handle_source_error(device_id, error))

    def _raise_startup_error(self) -> None:
        if self._startup_error is not None:
            raise self._startup_error

    def _handle_source_error(self, device_id: str, _error: Exception) -> None:
        with self._lock:
            if self._starting:
                if isinstance(_error, DeviceUnavailableError):
                    self._selection_invalid = True
                self._startup_error = _error
                return
            if device_id not in self._handles:
                return
            if isinstance(_error, DeviceUnavailableError):
                self._selection_invalid = True
        self.stop()
        if isinstance(_error, DeviceUnavailableError) and self._on_event is not None:
            self._on_event(
                CaptureEvent(
                    type="device_lost",
                    device_id=device_id,
                    message="The selected capture device was removed.",
                )
            )

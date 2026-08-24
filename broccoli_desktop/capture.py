"""Selected microphone and WASAPI-loopback capture without audio persistence."""

from __future__ import annotations

import logging
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

#: The usual Windows shared-mode mix rate. Devices open at their own native
#: rate (see _open_input); this constant only sizes the default block for
#: direct users of _QueuedCaptureHandle and for tests building 48 kHz blocks.
SAMPLE_RATE = 48_000
BLOCK_MS = 20
BLOCK_FRAMES = SAMPLE_RATE * BLOCK_MS // 1_000
BLOCK_BYTES = BLOCK_FRAMES * 2

#: Five seconds of headroom per source. The worker that drains this queue
#: shares the GIL with the rest of the process, and a transient stall beyond
#: the old one-second bound silently discarded meeting audio; at 20 ms blocks
#: the memory ceiling stays under ~1 MB per source even for stereo devices.
QUEUE_BLOCKS = 250

#: How often each source's watchdog polls stream health. Device removal has
#: no PortAudio callback signal; the poll is what notices a dead stream.
WATCHDOG_INTERVAL_SECONDS = 1.0

logger = logging.getLogger(__name__)

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

    def __init__(
        self,
        on_pcm: SourcePcmCallback,
        *,
        block_bytes: int = BLOCK_BYTES,
        channels: int = 1,
        watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
    ) -> None:
        self._on_pcm = on_pcm
        self._block_bytes = block_bytes
        self._channels = channels
        self._watchdog_interval_seconds = watchdog_interval_seconds
        self._queue: Queue[bytes | object] = Queue(maxsize=QUEUE_BLOCKS)
        self._stream: object | None = None
        self._closed = Event()
        self._error_handler: CaptureErrorCallback | None = None
        self._pending_error: Exception | None = None
        self._lock = RLock()
        # Bumped from the PortAudio callback thread, read from the worker;
        # plain ints under the GIL, and an off-by-one in a diagnostic is
        # cheaper than a lock on the audio path.
        self._flagged_blocks = 0
        self._mismatched_blocks = 0
        self._dropped_blocks = 0
        self._reported_degradation = False
        self._worker = Thread(target=self._run, name="broccoli-capture", daemon=True)
        self._worker.start()
        self._watchdog = Thread(
            target=self._watch_stream, name="broccoli-capture-watchdog", daemon=True
        )
        self._watchdog.start()

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
        """PortAudio callback: enqueue only, never call consumer or wait.

        A status flag here is an xrun -- an instant PortAudio already lost
        under load -- not a lost device, so the stream keeps running and the
        block that carried the flag is still real audio. Aborting on any
        flag used to end whole sessions on a single overflow; an actually
        removed endpoint is noticed by the watchdog instead.
        """
        if self._closed.is_set():
            return None, pyaudiowpatch.paAbort
        if status_flags:
            self._flagged_blocks += 1
        if len(pcm) == self._block_bytes:
            self._offer(pcm)
        else:
            self._mismatched_blocks += 1
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
        if self._watchdog is not current_thread():
            self._watchdog.join(timeout=1)

    def _offer(self, item: bytes | object) -> None:
        try:
            self._queue.put_nowait(item)
        except Full:
            if isinstance(item, bytes):
                self._dropped_blocks += 1
                return
            # _STOP must land even when the queue is full.
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
            if not isinstance(item, bytes):
                continue
            pcm = _downmix_to_mono(item, self._channels) if self._channels > 1 else item
            try:
                self._on_pcm(pcm)
            except Exception as error:
                self._report_error(error)
                return
            self._report_degradation_once()

    def _watch_stream(self) -> None:
        """Poll stream health so a dead endpoint still surfaces as device loss.

        PortAudio has no removal signal in its callback: when WASAPI
        invalidates an endpoint the processing thread just stops and
        Pa_IsStreamActive starts answering false. Reported directly, not
        through the PCM queue, so a full queue cannot delay or drop it.
        """
        while not self._closed.wait(self._watchdog_interval_seconds):
            stream = self._stream
            if stream is None:
                continue
            is_active = getattr(stream, "is_active", None)
            if is_active is None:
                return
            try:
                active = bool(is_active())
            except Exception:
                active = False
            if self._closed.is_set():
                return
            if not active:
                self._report_error(DeviceUnavailableError("capture device"))
                return

    def _report_degradation_once(self) -> None:
        """Say, once per source, that capture quality is being degraded.

        Transient flags and drops keep the session alive by design; what
        must not happen is the resulting transcript gaps staying invisible.
        """
        if self._reported_degradation:
            return
        if self._flagged_blocks or self._mismatched_blocks or self._dropped_blocks:
            self._reported_degradation = True
            logger.warning(
                "[capture] Degraded capture on one source: %d flagged, %d mismatched, "
                "%d dropped blocks so far.",
                self._flagged_blocks,
                self._mismatched_blocks,
                self._dropped_blocks,
            )

    def _report_error(self, error: Exception) -> None:
        with self._lock:
            on_error = self._error_handler
            if on_error is None:
                self._pending_error = error
                return
        on_error(error)


def _downmix_to_mono(pcm: bytes, channels: int) -> bytes:
    """Average interleaved PCM16 channels without retaining the sample data.

    Averaging keeps both sides of a stereo meeting mix; taking one channel
    of the mix format used to lose whatever the other one carried.
    """
    usable_samples = len(pcm) // 2 // channels * channels
    frames = numpy.frombuffer(pcm, dtype="<i2", count=usable_samples).reshape(-1, channels)
    mixed = numpy.rint(frames.astype(numpy.float32).mean(axis=1))
    return mixed.astype("<i2").tobytes()


class PyAudioCaptureBackend:
    """PyAudioWPatch adapter for direct mic and WASAPI output-loopback sources."""

    def __init__(
        self,
        pyaudio: object,
        *,
        watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
    ) -> None:
        self._pyaudio = pyaudio
        self._watchdog_interval_seconds = watchdog_interval_seconds

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
        return self._open_input(info, on_pcm, str(info["name"]))

    def open_loopback(self, device_id: str, on_pcm: SourcePcmCallback) -> CaptureHandle:
        output = self._find_info("system", device_id)
        try:
            loopback = self._pyaudio.get_wasapi_loopback_analogue_by_index(int(output["index"]))
        except (LookupError, OSError) as error:
            raise DeviceUnavailableError(str(output["name"])) from error
        return self._open_input(loopback, on_pcm, str(output["name"]))

    def _open_input(
        self, info: Mapping[str, object], on_pcm: SourcePcmCallback, display_name: str
    ) -> CaptureHandle:
        """Open one source at its own shared-mode mix format.

        WASAPI shared mode runs every stream at the endpoint's mix format;
        asking for a fixed 48 kHz mono either failed the open on 44.1 kHz
        and Bluetooth-headset endpoints or pushed the audio through an extra
        OS conversion. The worker downmixes multichannel blocks to mono, and
        the pipeline resamples from whatever rate the blocks arrive at.
        """
        rate = int(info["defaultSampleRate"])
        channels = max(1, int(info["maxInputChannels"]))
        if rate <= 0 or rate * BLOCK_MS % 1_000:
            # A rate that cannot form whole 20 ms blocks (11 025 Hz) would
            # silently drift the meeting clock by a fraction of every block.
            raise DeviceUnavailableError(display_name)
        frames_per_block = rate * BLOCK_MS // 1_000
        handle = _QueuedCaptureHandle(
            on_pcm,
            block_bytes=frames_per_block * channels * 2,
            channels=channels,
            watchdog_interval_seconds=self._watchdog_interval_seconds,
        )
        try:
            stream = self._pyaudio.open(
                format=pyaudiowpatch.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=int(info["index"]),
                frames_per_buffer=frames_per_block,
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
        """Yield WASAPI capture endpoints, once each.

        Windows also exposes every microphone through MME and DirectSound --
        "Microsoft Sound Mapper", names truncated to 31 characters, one
        duplicate per legacy API. Offering those routed the meeting through
        an emulation layer and an extra OS conversion; the outputs below
        were always WASAPI-only, and the microphones have to match.
        """
        for info in self._pyaudio.get_device_info_generator():
            if (
                int(info["maxInputChannels"]) > 0
                and not bool(info.get("isLoopbackDevice"))
                and self._is_wasapi(info)
            ):
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

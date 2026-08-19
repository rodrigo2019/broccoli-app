"""Selected microphone and WASAPI-loopback capture without audio persistence."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from hashlib import sha256
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread, current_thread
from typing import Literal, Protocol

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
    ) -> None:
        self._backend = backend
        self._microphone_id = microphone_id
        self._system_device_id = system_device_id
        self._on_pcm = on_pcm
        self._on_event = on_event
        selected_ids = {microphone_id, system_device_id}
        self._selected_labels = {
            device.device_id: device.label
            for device in backend.list_devices()
            if device.device_id in selected_ids
        }
        self._lock = RLock()
        self._handles: dict[str, CaptureHandle] = {}
        self._starting = False
        self._startup_error: Exception | None = None
        self._selection_invalid = False

    def start(self) -> None:
        """Start both sources or leave no active source behind."""
        with self._lock:
            if self._selection_invalid:
                raise DeviceUnavailableError("Select a replacement device")
            if self._handles:
                return
            labels = {device.device_id: device.label for device in self._backend.list_devices()}
            for device_id, label in labels.items():
                self._selected_labels.setdefault(device_id, label)
            opening_device_id = self._microphone_id
            self._starting = True
            self._startup_error = None
            try:
                microphone = self._backend.open_microphone(
                    self._microphone_id, lambda pcm: self._on_pcm("mic", pcm)
                )
                self._install_handle(self._microphone_id, microphone)
                self._raise_startup_error()
                opening_device_id = self._system_device_id
                loopback = self._backend.open_loopback(
                    self._system_device_id, lambda pcm: self._on_pcm("system", pcm)
                )
                self._install_handle(self._system_device_id, loopback)
                self._raise_startup_error()
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
        for handle in reversed(handles):
            handle.drain()
            handle.close()

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

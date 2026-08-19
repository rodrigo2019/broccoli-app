from __future__ import annotations

from threading import Event

import pytest

from broccoli_desktop.capture import (
    BLOCK_BYTES,
    BLOCK_FRAMES,
    SAMPLE_RATE,
    CaptureSession,
    DeviceUnavailableError,
    PyAudioCaptureBackend,
)
from tests.fakes import FakeCaptureBackend, FakePyAudio


@pytest.fixture
def fake_capture_backend() -> FakeCaptureBackend:
    return FakeCaptureBackend()


@pytest.fixture
def fake_pyaudio() -> FakePyAudio:
    return FakePyAudio()


def test_enumeration_exposes_microphones_and_loopback_outputs(
    fake_pyaudio: FakePyAudio,
) -> None:
    backend = PyAudioCaptureBackend(fake_pyaudio)

    devices = backend.list_devices()

    assert [device.kind for device in devices] == ["mic", "system"]
    assert [device.label for device in devices] == ["Microphone One", "Speakers"]
    assert [device.device_id.split(":", maxsplit=1)[0] for device in devices] == [
        "mic",
        "system",
    ]
    assert all(device.label not in device.device_id for device in devices)


def test_enumeration_uses_repeatable_ids_instead_of_display_names(
    fake_pyaudio: FakePyAudio,
) -> None:
    backend = PyAudioCaptureBackend(fake_pyaudio)

    first = backend.list_devices()
    second = backend.list_devices()

    assert [device.device_id for device in first] == [device.device_id for device in second]


def test_pyaudio_sources_use_48khz_mono_pcm16_20ms_blocks_and_dispatch_off_callback_thread(
    fake_pyaudio: FakePyAudio,
) -> None:
    delivered = Event()
    received: list[bytes] = []
    backend = PyAudioCaptureBackend(fake_pyaudio)

    microphone_id = backend.list_devices()[0].device_id
    handle = backend.open_microphone(
        microphone_id, lambda pcm: (received.append(pcm), delivered.set())
    )
    fake_pyaudio.streams[0].emit(b"\x01\x00" * BLOCK_FRAMES)

    assert delivered.wait(timeout=1)
    assert received == [b"\x01\x00" * BLOCK_FRAMES]
    assert {
        "rate": SAMPLE_RATE,
        "channels": 1,
        "format": 8,
        "input": True,
        "frames_per_buffer": BLOCK_FRAMES,
    }.items() <= fake_pyaudio.open_calls[0].items()
    handle.close()


def test_capture_session_stops_both_sources_when_one_callback_fails(
    fake_capture_backend: FakeCaptureBackend,
) -> None:
    fake_capture_backend.callback_pcm = b"\x00" * BLOCK_BYTES
    fake_capture_backend.callback_device_id = "system-1"

    def failing_callback(_: bytes) -> None:
        raise RuntimeError("send failed")

    session = CaptureSession(fake_capture_backend, "mic-1", "system-1", failing_callback)

    with pytest.raises(RuntimeError, match="send failed"):
        session.start()

    assert fake_capture_backend.closed_sources == {"mic-1", "system-1"}


def test_capture_session_closes_first_source_when_second_device_is_unavailable(
    fake_capture_backend: FakeCaptureBackend,
) -> None:
    fake_capture_backend.fail_opening = "system-1"
    session = CaptureSession(fake_capture_backend, "mic-1", "system-1", lambda _: None)

    with pytest.raises(DeviceUnavailableError, match="Speakers"):
        session.start()

    assert fake_capture_backend.closed_sources == {"mic-1"}


def test_capture_session_reports_the_microphone_label_when_it_cannot_open(
    fake_capture_backend: FakeCaptureBackend,
) -> None:
    fake_capture_backend.fail_opening = "mic-1"
    session = CaptureSession(fake_capture_backend, "mic-1", "system-1", lambda _: None)

    with pytest.raises(DeviceUnavailableError, match="Microphone One"):
        session.start()


def test_stop_is_idempotent_and_drains_both_sources(
    fake_capture_backend: FakeCaptureBackend,
) -> None:
    session = CaptureSession(fake_capture_backend, "mic-1", "system-1", lambda _: None)

    session.start()
    session.stop()
    session.stop()

    assert fake_capture_backend.closed_sources == {"mic-1", "system-1"}
    assert all(handle.drained for handle in fake_capture_backend.handles.values())


def test_device_loss_publishes_local_event_and_stops_capture(
    fake_capture_backend: FakeCaptureBackend,
) -> None:
    events = []
    session = CaptureSession(
        fake_capture_backend,
        "mic-1",
        "system-1",
        lambda _: None,
        on_event=events.append,
    )
    session.start()

    fake_capture_backend.handles["system-1"].lose_device()

    assert [event.type for event in events] == ["device_lost"]
    assert events[0].device_id == "system-1"
    assert fake_capture_backend.closed_sources == {"mic-1", "system-1"}


def test_device_loss_is_not_dropped_when_the_pcm_queue_is_full(fake_pyaudio: FakePyAudio) -> None:
    processing = Event()
    release_processing = Event()
    device_lost = Event()
    backend = PyAudioCaptureBackend(fake_pyaudio)
    devices = backend.list_devices()
    session = CaptureSession(
        backend,
        devices[0].device_id,
        devices[1].device_id,
        lambda _: (processing.set(), release_processing.wait(timeout=1)),
        on_event=lambda _: device_lost.set(),
    )
    session.start()

    microphone = fake_pyaudio.streams[0]
    microphone.emit(b"\x01\x00" * BLOCK_FRAMES)
    assert processing.wait(timeout=1)
    for _ in range(60):
        microphone.emit(b"\x01\x00" * BLOCK_FRAMES)
    microphone.emit(b"", status_flags=1)
    release_processing.set()

    assert device_lost.wait(timeout=1)


def test_consumer_failure_stops_capture_without_misreporting_device_loss(
    fake_pyaudio: FakePyAudio,
) -> None:
    processed = Event()
    device_lost = Event()
    backend = PyAudioCaptureBackend(fake_pyaudio)
    devices = backend.list_devices()

    def failing_consumer(_: bytes) -> None:
        processed.set()
        raise RuntimeError("send failed")

    session = CaptureSession(
        backend,
        devices[0].device_id,
        devices[1].device_id,
        failing_consumer,
        on_event=lambda _: device_lost.set(),
    )
    session.start()

    fake_pyaudio.streams[0].emit(b"\x01\x00" * BLOCK_FRAMES)

    assert processed.wait(timeout=1)
    assert not device_lost.wait(timeout=0.1)
    assert all(stream.closed for stream in fake_pyaudio.streams)

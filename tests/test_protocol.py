from __future__ import annotations

import pytest

from broccoli_desktop.models import AudioFrame
from broccoli_desktop.protocol import decode_audio_frame, encode_audio_frame


def test_encode_audio_frame_uses_the_listening_header() -> None:
    frame = encode_audio_frame(channel="mic", offset_ms=700, pcm=b"\x01\x00")

    assert frame[:2] == bytes([1, 0])
    assert int.from_bytes(frame[2:10], "big") == 700
    assert frame[10:] == b"\x01\x00"


def test_decode_audio_frame_round_trips_a_system_frame() -> None:
    encoded = bytes([1, 1]) + (12_345).to_bytes(8, "big") + b"\x01\x00\x02\x00"

    assert decode_audio_frame(encoded) == AudioFrame(
        channel="system", offset_ms=12_345, pcm=b"\x01\x00\x02\x00"
    )


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (bytes([2, 0]) + (0).to_bytes(8, "big"), "Unsupported audio protocol version."),
        (bytes([1, 2]) + (0).to_bytes(8, "big"), "Unknown audio channel."),
        (bytes([1, 0]) + (0).to_bytes(8, "big") + b"\x01", "PCM payload must be PCM16."),
    ],
)
def test_decode_audio_frame_rejects_invalid_protocol_data(frame: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        decode_audio_frame(frame)


def test_encode_audio_frame_rejects_negative_offsets() -> None:
    with pytest.raises(ValueError, match="offset_ms must be non-negative"):
        encode_audio_frame(channel="mic", offset_ms=-1, pcm=b"")

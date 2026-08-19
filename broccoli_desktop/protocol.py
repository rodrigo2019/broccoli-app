"""Versioned binary codec for Listening audio frames."""

from __future__ import annotations

from typing import Literal

from broccoli_desktop.models import AudioFrame

PROTOCOL_VERSION = 1
HEADER_SIZE = 10
CHANNEL_BYTES: dict[Literal["mic", "system"], int] = {"mic": 0, "system": 1}
BYTE_CHANNELS = {value: key for key, value in CHANNEL_BYTES.items()}


def encode_audio_frame(*, channel: Literal["mic", "system"], offset_ms: int, pcm: bytes) -> bytes:
    """Encode one PCM16 frame for the remote Listening protocol."""
    if offset_ms < 0:
        raise ValueError("offset_ms must be non-negative")
    if len(pcm) % 2:
        raise ValueError("PCM payload must be PCM16.")
    try:
        channel_byte = CHANNEL_BYTES[channel]
    except KeyError:
        raise ValueError("Unknown audio channel.") from None
    return bytes([PROTOCOL_VERSION, channel_byte]) + offset_ms.to_bytes(8, "big") + pcm


def decode_audio_frame(frame: bytes) -> AudioFrame:
    """Decode a local diagnostic copy of a version-1 Listening frame."""
    if len(frame) < HEADER_SIZE:
        raise ValueError("Audio frame is incomplete.")
    if frame[0] != PROTOCOL_VERSION:
        raise ValueError("Unsupported audio protocol version.")
    try:
        channel = BYTE_CHANNELS[frame[1]]
    except KeyError:
        raise ValueError("Unknown audio channel.") from None
    pcm = frame[HEADER_SIZE:]
    if len(pcm) % 2:
        raise ValueError("PCM payload must be PCM16.")
    return AudioFrame(channel=channel, offset_ms=int.from_bytes(frame[2:10], "big"), pcm=pcm)

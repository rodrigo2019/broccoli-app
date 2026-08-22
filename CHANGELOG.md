# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Forced transcription language per channel, chosen in the capture dock before a capture starts.
- Mute for the microphone and the system audio, toggleable during a capture.
- Session pinning to keep important sessions at the top of the library.
- Session deletion from the session library.
- Automatic session naming.
- Audio level metering with a per-channel histogram.
- Session search.
- Infinite scroll in the session history.

## [0.1.0] - 2026-08-19

### Added

- Windows desktop capture with separate microphone and system-loopback channels.
- Session library, title editing, transcript history, and historical-session resume.
- System tray controls for showing the window, stopping capture, and quitting safely.
- Reproducible PyInstaller onedir packaging, Inno Setup installation, and Windows CI artifacts.

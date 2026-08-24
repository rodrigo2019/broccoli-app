# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Proxy configuration through an automatic configuration script (PAC), alongside
  the existing manual host and port. Windows evaluates the script and chooses a
  proxy per destination; the settings screen arrives with the corporate script
  address filled in, and the proxy stays off until it is switched on.
- The Broccoli logo on every Windows surface: the window title bar and its taskbar
  button, the notification area, `BroccoliDesktop.exe` and the shortcuts it backs,
  and the installer. `scripts/build_icon.py` rasterizes the icon from the logo SVG.
- An explicit AppUserModelID, claimed at startup and repeated on the installed
  shortcuts. The taskbar resolves a button's icon through that identity rather
  than reading the window's own, so without it the button showed the icon of
  whichever executable launched the process.
- Forced transcription language per channel, chosen in the capture dock before a capture starts.
- Mute for the microphone and the system audio, toggleable during a capture.
- Session pinning to keep important sessions at the top of the library.
- Session deletion from the session library.
- Automatic session naming.
- Audio level metering with a per-channel histogram.
- Session search.
- Infinite scroll in the session history.

### Fixed

- The packaged application starts. It never had: `__main__` reaches the runtime
  through `import_module`, whose argument PyInstaller cannot follow, so nothing
  below it was ever collected into the archive. The spec now collects the
  package's submodules.
- The packaged application no longer dies configuring its logging. A windowed
  build has no console, so Python leaves `sys.stdout` and `sys.stderr` as None
  and uvicorn's formatter raised asking one of them whether it is a terminal.
  Both failures were invisible to a build that only had to compile, so CI now
  launches the executable and waits for its local service to answer.

## [0.1.0] - 2026-08-19

### Added

- Windows desktop capture with separate microphone and system-loopback channels.
- Session library, title editing, transcript history, and historical-session resume.
- System tray controls for showing the window, stopping capture, and quitting safely.
- Reproducible PyInstaller onedir packaging, Inno Setup installation, and Windows CI artifacts.

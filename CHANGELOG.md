# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The notification area's "Abrir o Broccoli Desktop" now opens the window.
  Every route back to a hidden window ended in a call to PyWebView's `focus`,
  which is a constructor flag rather than a method, so the click raised
  `TypeError` and left the window where it was.
- Transcription quality: audio reaching the transcription model no longer
  carries resampling artifacts. Each 20 ms capture block was converted in
  isolation, restarting the resampler's filter 50 times a second and smearing
  edge transients over all speech (~31 dB SNR against the continuous
  conversion; ~70 dB now that one stateful stream per channel spans blocks).
- A transient input overflow no longer ends the capture as a lost device. The
  flagged block is kept, the degradation is logged, and actual device removal
  is detected by a per-source watchdog on stream health instead.
- Reconnects longer than ten seconds no longer silently discard most of the
  audio captured while offline: the reconnect buffer now holds minutes, and
  when it does trim, the user is told.

### Changed

- Capture devices open at their own shared-mode mix format instead of a forced
  48 kHz mono. Endpoints at other rates (44.1 kHz interfaces, Bluetooth
  headsets) previously failed to open or passed through an extra OS
  conversion; stereo mixes are now averaged to mono instead of losing a side.
- The microphone list offers only WASAPI endpoints. The MME/DirectSound
  duplicates ("Microsoft Sound Mapper", names truncated to 31 characters)
  routed capture through legacy emulation layers.

### Added

- English, Portuguese (Brazil) and German, chosen in the settings screen and
  applied everywhere the application speaks: the window, the notification-area
  menu and its status line, and the Windows dialogs -- without a restart and
  without reloading the window. A fresh install follows the Windows display
  language and falls back to English; "Follow Windows" in the picker goes back
  to that. German is also offered as a forced transcription language per
  channel. The automatic titles given to unnamed sessions stay in Portuguese by
  choice: a title is written into the session when it is created and travels
  with it as data, so translating it would change only the next one and leave a
  history reading in two languages at once.
- One running copy per environment. A second launch no longer opens a second
  window, a second tray icon, and a second capture competing for the same audio
  device: it brings the window that is already running forward -- back from the
  notification area, at the size it was left -- and exits. Production, `--dev`
  and `--local` hold separate locks, so a checkout still opens beside an
  installed build. The installer reads the same lock, and asks for the
  application to be closed rather than writing over files it has open.
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

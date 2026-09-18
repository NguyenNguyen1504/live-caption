# Live Caption

A focused Windows desktop client for the local real-time API documented in
[API.md](API.md). It renders progressive, YouTube-style captions in a
bottom-center, always-on-top overlay and is controlled from the Windows system
tray.

The application starts in **Demo Mode**, so it can be visually tested without
the main application, a microphone, an ASR model, an API server, or a token.
Demo and Real API events use the same revision-aware caption state.

## Test it on Windows

With Python 3.12 or newer installed, open PowerShell in this repository:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Then double-click:

```text
.venv\Scripts\LocalDictationCaptions.exe
```

It launches without a console and immediately shows progressive demo captions.
Right-click the notification-area icon to stop/start captions, switch between
Demo Mode and Real API Mode, configure the API connection, or exit.

## Build one executable

Run on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows_app.ps1
```

Then double-click `dist\LocalDictationCaptions.exe`. PyInstaller does not
cross-compile, so the executable must be built on Windows.

## Real API Mode

Configure a loopback endpoint (normally `http://127.0.0.1:8765`) and a paired
token containing `status:read` plus `transcript:live`. The token is stored with
the operating-system credential service. The client uses only the documented
HTTP status and WebSocket event endpoints; it imports no code from the main
application.

If the API host is unavailable, the app waits and reconnects without crashing,
and Demo Mode remains available. Real progressive captions depend on the host
reporting `partials_available: true` and emitting `transcript.partial` events.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
```

See [docs/WINDOWS_CAPTIONS.md](docs/WINDOWS_CAPTIONS.md) for complete usage,
security, behavior, and packaging notes.

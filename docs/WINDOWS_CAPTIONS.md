# Windows live captions

Local Dictation Captions is a complete, tray-controlled Windows application.
It starts in **Demo Mode**, so the visual experience can be tested immediately
without a microphone, model, API token, or running dictation host.

The caption overlay shows at most two recent lines at the bottom-center of the
primary Windows work area. It is always on top, absent from the taskbar and
Alt-Tab, does not take keyboard focus, and lets mouse clicks pass through it.

## Fastest way to test Demo Mode

On a Windows 10 or 11 machine with Python 3.12 from python.org:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Then double-click:

```text
.venv\Scripts\LocalDictationCaptions.exe
```

This is a no-console Windows launcher created by the package installation. The
application starts captions automatically in Demo Mode. After a short pause,
the bottom of the screen progresses through captions such as:

```text
Hello everyone
Hello everyone, today we're
Hello everyone, today we're going to talk about
Hello everyone, today we're going to talk about live captions.
```

It then finalizes that caption, clears it, starts a second caption, and repeats.
All demo events pass through the same revision-aware `CaptionState` used by the
real API source.

## Controls

Look for the application icon in the Windows notification area. Right-click it
for:

- **Start captions** / **Stop captions**
- **Demo Mode**
- **Real API Mode**
- **Configure Real API…**
- **Exit**

Double-clicking the tray icon also starts or stops captions. If Windows cannot
create the tray icon, the application displays a small fallback control window.
The caption overlay itself never contains controls or window chrome.

## Build a standalone executable

Run this from PowerShell at the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows_app.ps1
```

The script installs the packaging dependency and creates:

```text
dist\LocalDictationCaptions.exe
```

That is a single-file, windowed executable. Double-click it on the same Windows
machine to launch directly into Demo Mode; no terminal is opened. PyInstaller
does not cross-compile, so this build script must be run on Windows.

## Real API Mode

Real API Mode retains the version-1 loopback API integration in `API.md`. Open
the tray menu, select **Configure Real API…**, and enter:

- the loopback endpoint, normally `http://127.0.0.1:8765`;
- a paired token with `status:read` and `transcript:live`.

The token is stored in Windows Credential Manager, never in a plaintext config
file or command-line argument. The endpoint is restricted to a loopback address
so the token cannot be sent to a remote host. The client also bypasses HTTP
proxy settings for this local request.

Select **Real API Mode** from the tray. If the host is unavailable, the overlay
reports that it is reconnecting and the tray remains usable, so Demo Mode can
be selected at any time. Authentication and scope failures are not retried,
avoiding the API's shared failed-authentication rate limit.

This standalone repository does not provide microphone capture or ASR. Those
belong to the API host and affect Real API Mode only; they do not block the
packaged GUI or Demo Mode.

### Live revision behavior

The API host's ASR segment and the overlay's mutable caption segment are
intentionally separate. Every partial is displayed as soon as the client
receives it, while `CaptionState` commits shorter display chunks using only
text already present in that partial:

- sentence punctuation commits immediately;
- a clause boundary already present near the 10-word soft target is preferred;
- without punctuation, the mutable suffix can grow to 16 words, then a
  10-word prefix is committed;
- a new upstream `segment_id` commits the previous remainder.

Later partial revisions replace only the active suffix. Committed display
chunks stay stable until the API sends the canonical `transcript.final`, which
supersedes all provisional text as required by the version-1 contract. These
display boundaries do not restart the recognizer, change its audio/context
window, add a timeout, or wait for punctuation or silence.

## Automated tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
```

These tests cover immediate and rapid partial display, punctuation and hard
boundaries, active autocorrection, stable committed chunks, insertion/deletion
alignment without duplicated or dropped words, stale revisions, final
supersession, demo events, wrapping, scope validation, a real local WebSocket
connection, and idle ping/pong.

## Privacy and behavior

- Real transcript events are unredacted. The application does not log or
  persist caption text.
- A stopped caption remains visible briefly; cancellation clears it immediately.
- A reconnect clears provisional text because API version 1 has no replay.
- Real progressive captions require `partials_available: true` from the host.
- The current overlay targets the primary Windows work area.

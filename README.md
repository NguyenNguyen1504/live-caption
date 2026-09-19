# Live Caption

A focused Windows desktop client for the local real-time API documented in
[API.md](API.md). It renders progressive captions in a bottom-center,
always-on-top overlay controlled from the Windows system tray. The captions are
styled after YouTube's automatic captions: white proportional sans-serif on a
translucent black box per line, two lines at most, words revealed one at a
time, and older lines rolling up.

## Windows Quick Start

### 1. Try the app with Demo Mode

Demo Mode uses built-in fake transcription events to exercise the real caption
UI and caption-processing pipeline. It does **not** require the main dictation
app, an API server, a microphone, or an API token.

On a Windows 10 or 11 computer, install Git and Python 3.12, then open
PowerShell and run:

```powershell
git clone https://github.com/NguyenNguyen1504/live-caption.git
cd live-caption
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_app.ps1
```

The build creates:

```text
dist\LocalDictationCaptions.exe
```

Double-click that file. Demo Mode starts automatically, and captions should
appear near the bottom-center of the screen.

The application runs primarily through its Windows system-tray icon. The icon
may be hidden inside the notification-area overflow menu. Right-click it for:

- **Start captions** or **Stop captions**
- **Demo Mode**
- **Real API Mode**
- **Configure Real API…**
- **Exit**

The executable is currently unsigned, so Windows SmartScreen may call it an
unrecognized app. If you trust the executable you built locally, select
**More info**, then **Run anyway**.

After the build, you can copy `LocalDictationCaptions.exe` to another compatible
Windows computer. That computer does not need Python, Git, or this source
repository. Build and destination computers should use the same CPU
architecture.

### 2. Switch from Demo Mode to Real API Mode

Demo Mode tests the standalone Windows application, but it does not test
end-to-end communication with the main dictation app. Real API Mode receives
actual transcription events from that app through its local HTTP/WebSocket API:

```text
Demo Mode:
built-in fake transcription -> caption pipeline -> overlay

Real API Mode:
main app / STT -> API/WebSocket -> caption pipeline -> overlay
```

To use Real API Mode:

1. Enable and start the main dictation application's local API on the same
   computer.
2. In the main application, create a paired token with the `status:read` and
   `transcript:live` scopes. The current API documents this command:

   ```powershell
   dictation api pair "Caption overlay" --scope transcript:live --scope status:read
   ```

3. Start `LocalDictationCaptions.exe`.
4. Right-click the Live Caption tray icon and select **Real API Mode**.
5. If no connection is saved, the **Real API connection** window opens. Keep
   the default endpoint `http://127.0.0.1:8765` unless the main application uses
   another documented loopback port, paste the paired token, and select
   **Save and connect**. The token is stored in Windows Credential Manager.
6. Select **Start captions** from the tray if captions are stopped.
7. Start a recording in the main application and speak. Real transcription
   events should appear in the overlay.

You can change the saved endpoint or token later with **Configure Real API…**
in the tray menu. Real progressive captions require the API host to provide
`transcript.partial` events.

### Optional: Development launch

Developers can run directly from the repository instead of building the
standalone executable:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Start-Process .\.venv\Scripts\LocalDictationCaptions.exe
```

## Automated tests

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

See [docs/WINDOWS_CAPTIONS.md](docs/WINDOWS_CAPTIONS.md) for complete usage,
security, behavior, and packaging notes.

"""Non-activating, always-on-top Windows caption overlay."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
from ctypes import wintypes
from typing import Any

from live_caption.client import Endpoint, LocalApiClient
from live_caption.credentials import (
    DEFAULT_ENDPOINT,
    CaptionCredentials,
    CredentialError,
)
from live_caption.model import CaptionState
from live_caption.source import DemoWorker, Notice, StreamWorker

CAPTION_HOLD_MS = 2500


class CaptionOverlay:
    """Complete tray-controlled caption application and borderless overlay."""

    def __init__(
        self,
        credentials: CaptionCredentials | None,
        *,
        initial_mode: str = "demo",
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.root = tk.Tk(className="LocalDictationCaptions")
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.transparent = "#010203"
        self.root.configure(background=self.transparent)
        if sys.platform == "win32":
            self.root.wm_attributes("-transparentcolor", self.transparent)

        self.font = tkfont.Font(family="Segoe UI", size=22, weight="bold")
        self.label = tk.Label(
            self.root,
            background="#080808",
            foreground="white",
            font=self.font,
            justify="center",
            padx=12,
            pady=6,
            borderwidth=0,
        )
        self.label.pack()
        self.state = CaptionState()
        self.notices: queue.SimpleQueue[Notice] = queue.SimpleQueue()
        self.commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.credentials = credentials
        self.mode = initial_mode if initial_mode in {"demo", "real"} else "demo"
        self.running = False
        self._closed = False
        self._source_generation = 0
        self._source_stop: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._tray: Any = None
        self._settings_window: Any = None
        self._fallback_window: Any = None
        self._fallback_toggle: Any = None
        self._hide_generation = 0
        self._max_width = max(320, int(self.root.winfo_screenwidth() * 0.78))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def run(self) -> None:
        self.root.update_idletasks()
        _make_nonactivating(self.root.winfo_id())
        from live_caption.tray import WindowsTray

        self._tray = WindowsTray(self.commands.put)
        if not self._tray.start():
            self._show_fallback_controls()
        self.root.after(16, self._poll)
        self.root.after(50, self.start_captions)
        try:
            self.root.mainloop()
        finally:
            self._retire_source()
            if self._tray is not None:
                self._tray.stop()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._retire_source()
        if self._tray is not None:
            self._tray.stop()
        self.root.destroy()

    def start_captions(self) -> None:
        if self.running:
            return
        if self.mode == "real":
            try:
                client = self._configured_client()
            except (CredentialError, ValueError):
                self._show_api_settings(connect_after_save=True)
                return
            self.running = True
            self._replace_source(client)
        else:
            self.running = True
            self._replace_source()
        self._update_controls()

    def stop_captions(self) -> None:
        if not self.running:
            return
        self.running = False
        self._retire_source()
        self.state.reset()
        self._show("")
        self._update_controls()

    def _select_demo(self) -> None:
        if self.mode == "demo":
            return
        self.mode = "demo"
        if self.running:
            self._replace_source()
        self._update_controls()

    def _select_real(self) -> None:
        try:
            client = self._configured_client()
        except (CredentialError, ValueError):
            self._show_api_settings(connect_after_save=True)
            return
        self.mode = "real"
        if self.running:
            self._replace_source(client)
        self._update_controls()

    def _configured_client(self) -> LocalApiClient:
        if self.credentials is None:
            raise CredentialError("Windows Credential Manager is unavailable")
        endpoint_text = self.credentials.endpoint()
        token = self.credentials.token(endpoint_text)
        if not token:
            raise CredentialError("no Real API token is configured")
        return LocalApiClient(Endpoint.parse(endpoint_text), token)

    def _replace_source(self, client: LocalApiClient | None = None) -> None:
        self._retire_source()
        self.state.reset()
        self._show("")
        stop_event = threading.Event()
        self._source_stop = stop_event
        if self.mode == "demo":
            worker: threading.Thread = DemoWorker(
                self.notices, stop_event, self._source_generation
            )
        else:
            if client is None:
                client = self._configured_client()
            worker = StreamWorker(
                client, self.notices, stop_event, self._source_generation
            )
        self._worker = worker
        worker.start()

    def _retire_source(self) -> None:
        if self._source_stop is not None:
            self._source_stop.set()
        self._source_stop = None
        self._worker = None
        self._source_generation += 1

    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.notices.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self._handle_command(self.commands.get_nowait())
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(16, self._poll)

    def _handle(self, notice: Notice) -> None:
        if notice.generation != self._source_generation or not self.running:
            return
        if notice.kind == "sync":
            self.state.sync(notice.value if isinstance(notice.value, str) else "")
            self._show("")
            return
        if notice.kind == "event" and isinstance(notice.value, dict):
            event = notice.value
            text = self.state.apply(event)
            if text is not None:
                self._show(text)
            if (
                event.get("event") == "session.stopped"
                and event.get("session_id") == self.state.session_id
            ):
                self._schedule_hide()
            return
        if notice.kind == "disconnected":
            # Provisional output cannot be trusted after a gap; v1 has no replay.
            self.state.reset()
            self._show(str(notice.value))
            return
        if notice.kind == "fatal":
            self.state.reset()
            self._show(str(notice.value))

    def _handle_command(self, command: str) -> None:
        if command == "toggle":
            self.stop_captions() if self.running else self.start_captions()
        elif command == "demo":
            self._select_demo()
        elif command == "real":
            self._select_real()
        elif command == "configure":
            self._show_api_settings(connect_after_save=False)
        elif command == "exit":
            self.close()

    def _update_controls(self) -> None:
        if self._tray is not None:
            self._tray.update(running=self.running, mode=self.mode)
        if self._fallback_toggle is not None:
            self._fallback_toggle.set("Stop captions" if self.running else "Start captions")

    def _show_api_settings(self, *, connect_after_save: bool) -> None:
        from tkinter import messagebox, ttk

        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.lift()
            self._settings_window.focus_force()
            return
        window = self.tk.Toplevel(self.root)
        self._settings_window = window
        window.title("Real API connection")
        window.resizable(False, False)
        window.attributes("-topmost", True)
        window.protocol("WM_DELETE_WINDOW", window.destroy)

        try:
            current_endpoint = self.credentials.endpoint() if self.credentials else DEFAULT_ENDPOINT
        except CredentialError:
            current_endpoint = DEFAULT_ENDPOINT
        endpoint = self.tk.StringVar(value=current_endpoint)
        token = self.tk.StringVar()
        frame = ttk.Frame(window, padding=16)
        frame.grid()
        ttk.Label(frame, text="Loopback API endpoint").grid(row=0, column=0, sticky="w")
        endpoint_entry = ttk.Entry(frame, textvariable=endpoint, width=42)
        endpoint_entry.grid(row=1, column=0, columnspan=2, pady=(3, 12), sticky="ew")
        ttk.Label(frame, text="Paired token").grid(row=2, column=0, sticky="w")
        token_entry = ttk.Entry(frame, textvariable=token, show="●", width=42)
        token_entry.grid(row=3, column=0, columnspan=2, pady=(3, 4), sticky="ew")
        ttk.Label(
            frame,
            text="Leave blank to keep the token already stored for this endpoint.",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 14))

        def save() -> None:
            if self.credentials is None:
                messagebox.showerror(
                    "Credential store unavailable",
                    "Windows Credential Manager is unavailable on this system.",
                    parent=window,
                )
                return
            try:
                endpoint_value = endpoint.get().strip()
                Endpoint.parse(endpoint_value)
                existing = self.credentials.token(endpoint_value)
                if not token.get().strip() and not existing:
                    raise CredentialError("Paste a paired API token.")
                self.credentials.save(endpoint_value, token.get())
            except (CredentialError, ValueError) as error:
                messagebox.showerror("Cannot save API connection", str(error), parent=window)
                return
            window.destroy()
            self._settings_window = None
            if connect_after_save or (self.running and self.mode == "real"):
                self.mode = "real"
                if not self.running:
                    self.running = True
                self._replace_source(self._configured_client())
                self._update_controls()

        ttk.Button(frame, text="Cancel", command=window.destroy).grid(
            row=5, column=0, padx=(0, 8), sticky="e"
        )
        save_label = "Save and connect" if connect_after_save else "Save"
        ttk.Button(frame, text=save_label, command=save).grid(
            row=5, column=1, sticky="e"
        )
        endpoint_entry.focus_set()
        window.update_idletasks()
        x = (window.winfo_screenwidth() - window.winfo_reqwidth()) // 2
        y = (window.winfo_screenheight() - window.winfo_reqheight()) // 2
        window.geometry(f"+{x}+{y}")

    def _show_fallback_controls(self) -> None:
        from tkinter import ttk

        window = self.tk.Toplevel(self.root)
        self._fallback_window = window
        window.title("Live Captions")
        window.attributes("-topmost", True)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(window, padding=10)
        frame.grid()
        self._fallback_toggle = self.tk.StringVar(value="Start captions")
        ttk.Button(
            frame,
            textvariable=self._fallback_toggle,
            command=lambda: self._handle_command("toggle"),
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(frame, text="Demo Mode", command=self._select_demo).grid(
            row=1, column=0, padx=(0, 4)
        )
        ttk.Button(frame, text="Real API Mode", command=self._select_real).grid(
            row=1, column=1, padx=(4, 0)
        )
        ttk.Button(frame, text="Exit", command=self.close).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )

    def _show(self, text: str) -> None:
        self._hide_generation += 1
        if not text:
            self.root.withdraw()
            return
        recent = fit_recent_lines(text, self.font.measure, self._max_width, max_lines=2)
        self.label.configure(text=recent)
        self.root.update_idletasks()
        width = min(self.label.winfo_reqwidth(), self._max_width + 24)
        height = self.label.winfo_reqheight()
        left, top, right, bottom = _work_area(self.root)
        x = left + max(0, (right - left - width) // 2)
        y = top + max(0, bottom - top - height - 36)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()

    def _schedule_hide(self) -> None:
        self._hide_generation += 1
        generation = self._hide_generation

        def hide_if_unchanged() -> None:
            if generation == self._hide_generation:
                self.state.reset()
                self.root.withdraw()

        self.root.after(CAPTION_HOLD_MS, hide_if_unchanged)


def fit_recent_lines(
    text: str,
    measure: Any,
    max_width: int,
    *,
    max_lines: int,
) -> str:
    """Word-wrap text and retain only the most recent visible lines."""
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and measure(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines[-max_lines:])


def _make_nonactivating(window_id: int) -> None:
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    long_pointer = ctypes.c_ssize_t
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = long_pointer
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, long_pointer]
    set_style.restype = long_pointer
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    ex_style = get_style(window_id, -20)
    # Tool window: no taskbar/Alt-Tab entry. No-activate and transparent: never
    # steal keyboard focus, and mouse clicks pass through to the app beneath.
    set_style(window_id, -20, ex_style | 0x00000080 | 0x08000000 | 0x00000020)
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0020
    user32.SetWindowPos(window_id, wintypes.HWND(-1), 0, 0, 0, 0, flags)


def _work_area(root: Any) -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    rectangle = wintypes.RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rectangle), 0):
        return rectangle.left, rectangle.top, rectangle.right, rectangle.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()

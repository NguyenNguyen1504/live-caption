"""Non-activating, always-on-top Windows caption overlay.

The presentation deliberately copies YouTube's automatic captions: a
proportional sans-serif face, white text with no character edge, a translucent
black box hugging each line separately, at most two lines, words revealed one
at a time, and a roll-up whose bottom edge stays anchored above the taskbar.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Sequence

from live_caption.client import Endpoint, LocalApiClient
from live_caption.credentials import (
    DEFAULT_ENDPOINT,
    CaptionCredentials,
    CredentialError,
)
from live_caption.model import CaptionState
from live_caption.source import DemoWorker, Notice, StreamWorker

CAPTION_HOLD_MS = 2500

# YouTube's default caption style: "Proportional Sans-Serif" (Roboto), 100%
# white text, a black background at 75% opacity, and no character edge.
CAPTION_FONT_CANDIDATES = ("Roboto", "Arial", "Helvetica", "Segoe UI")
CAPTION_FONT_SIZE = 24
CAPTION_FOREGROUND = "#ffffff"
CAPTION_BACKGROUND = "#000000"
CAPTION_PAD_X = 10
CAPTION_PAD_Y = 3
# A layered window applies one alpha to text and box alike, so this sits above
# YouTube's 75% background to keep the glyphs themselves crisp.
CAPTION_ALPHA = 0.85
CAPTION_LINES = 2
# YouTube breaks automatic captions into short lines instead of filling the
# player. Measuring an average-width sample keeps that rhythm at any font size
# or DPI, and the screen fraction caps it on a narrow display.
CAPTION_LINE_CHARACTERS = 45
CAPTION_WIDTH_SAMPLE = "the quick brown fox jumps over the lazy dog"
CAPTION_WIDTH_FRACTION = 0.62
CAPTION_BOTTOM_FRACTION = 0.06
CAPTION_BOTTOM_MIN = 40
# YouTube appends one recognized word at a time. Pacing the reveal makes a
# multi-word revision cascade in rather than snap in as a block.
REVEAL_INTERVAL_MS = 45
REVEAL_CATCHUP = 3
FADE_MS = 200
FADE_STEPS = 8


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

        self.font = tkfont.Font(
            family=_caption_family(tkfont), size=CAPTION_FONT_SIZE, weight="normal"
        )
        # One label per caption line: each box is only as wide as its own text.
        self.body = tk.Frame(self.root, background=self.transparent)
        self.body.pack(fill="both", expand=True)
        self._lines: list[Any] = []
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
        self._fade_generation = 0
        self._target_words: tuple[str, ...] = ()
        self._revealed = 0
        self._last_reveal = 0.0
        self._max_width = _line_width(self.font, self.root.winfo_screenwidth())
        self.layout = RollUpLines(self.font.measure, self._max_width)
        # Claim the layered window's alpha before the extended styles are set,
        # so a later fade only rewrites the alpha byte.
        self._set_alpha(CAPTION_ALPHA)
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
        self._advance_reveal()
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
        words = tuple(text.split())
        if not words:
            self._target_words = ()
            self._revealed = 0
            self.layout.reset()
            self._hide_now()
            return
        self._target_words = words
        # A revision can shorten the transcript; never reveal past its end.
        self._revealed = min(self._revealed, len(words))
        if self._revealed == 0:
            # The first word of an utterance is never held back.
            self._revealed = 1
            self._last_reveal = time.monotonic()
        self._render()

    def _advance_reveal(self) -> None:
        """Let queued words appear one at a time, the way YouTube types them."""
        pending = len(self._target_words) - self._revealed
        if pending <= 0:
            return
        now = time.monotonic()
        if (now - self._last_reveal) * 1000 < REVEAL_INTERVAL_MS:
            return
        self._last_reveal = now
        # Catch up proportionally so a burst never falls behind the speaker.
        self._revealed += max(1, pending // REVEAL_CATCHUP)
        self._render()

    def _render(self) -> None:
        lines = self.layout.lines(self._target_words[: self._revealed])
        if not lines:
            self._hide_now()
            return
        self._present(lines)

    def _present(self, lines: list[str]) -> None:
        self._sync_line_widgets(lines)
        self.root.update_idletasks()
        left, top, right, bottom = _work_area(self.root)
        width = min(self.body.winfo_reqwidth(), right - left)
        height = self.body.winfo_reqheight()
        margin = max(CAPTION_BOTTOM_MIN, int((bottom - top) * CAPTION_BOTTOM_FRACTION))
        x = left + max(0, (right - left - width) // 2)
        # Bottom-anchored, so a second line grows upward instead of pushing the
        # current line down.
        y = top + max(0, bottom - top - height - margin)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self._fade_generation += 1
        self._set_alpha(CAPTION_ALPHA)
        self.root.deiconify()
        self.root.lift()

    def _sync_line_widgets(self, lines: list[str]) -> None:
        while len(self._lines) < len(lines):
            self._lines.append(
                self.tk.Label(
                    self.body,
                    background=CAPTION_BACKGROUND,
                    foreground=CAPTION_FOREGROUND,
                    font=self.font,
                    justify="center",
                    padx=CAPTION_PAD_X,
                    pady=CAPTION_PAD_Y,
                    borderwidth=0,
                )
            )
        for index, label in enumerate(self._lines):
            if index < len(lines):
                label.configure(text=lines[index])
                # No gap between boxes: consecutive lines touch, as on YouTube.
                label.pack(side="top", anchor="center", pady=0)
            else:
                label.pack_forget()

    def _set_alpha(self, value: float) -> None:
        try:
            self.root.attributes("-alpha", max(0.0, min(1.0, value)))
        except self.tk.TclError:
            pass

    def _hide_now(self) -> None:
        self._fade_generation += 1
        self.root.withdraw()
        self._set_alpha(CAPTION_ALPHA)

    def _fade_out(self, step: int, generation: int) -> None:
        if self._closed or generation != self._fade_generation:
            return
        if step >= FADE_STEPS:
            self._hide_now()
            return
        self._set_alpha(CAPTION_ALPHA * (1 - (step + 1) / FADE_STEPS))
        self.root.after(
            max(1, FADE_MS // FADE_STEPS),
            lambda: self._fade_out(step + 1, generation),
        )

    def _schedule_hide(self) -> None:
        self._hide_generation += 1
        generation = self._hide_generation

        def fade_if_unchanged() -> None:
            if generation != self._hide_generation:
                return
            self.state.reset()
            self._target_words = ()
            self._revealed = 0
            self.layout.reset()
            self._fade_generation += 1
            self._fade_out(0, self._fade_generation)

        self.root.after(CAPTION_HOLD_MS, fade_if_unchanged)


class RollUpLines:
    """YouTube-style roll-up layout for a growing, revisable transcript.

    A line is frozen as soon as the next word no longer fits it, so text that
    has already rolled up never reflows: only the bottom line grows, exactly
    like an automatic caption track. Revisions to the mutable tail are
    absorbed; a transcript that no longer matches the frozen lines is re-laid
    out from scratch.
    """

    def __init__(
        self, measure: Any, max_width: int, *, max_lines: int = CAPTION_LINES
    ) -> None:
        self.measure = measure
        self.max_width = max_width
        self.max_lines = max_lines
        self._locked: list[list[str]] = []
        # Words dropped with lines that have scrolled off the top. Keeping the
        # count bounds the work per update to the visible caption, not to the
        # whole session transcript.
        self._dropped = 0

    def reset(self) -> None:
        self._locked = []
        self._dropped = 0

    def lines(self, words: Sequence[str]) -> list[str]:
        consumed = self._dropped + sum(len(line) for line in self._locked)
        if not self._holds(words, consumed):
            self.reset()
            consumed = 0
        wrapped = wrap_words(words[consumed:], self.measure, self.max_width)
        if len(wrapped) > 1:
            # Every finished line but the last is frozen: the newest word
            # starts a fresh bottom line and older lines roll up unchanged.
            self._locked.extend(wrapped[:-1])
            while len(self._locked) > self.max_lines:
                self._dropped += len(self._locked.pop(0))
        visible = (self._locked + wrapped[-1:])[-self.max_lines :]
        return [" ".join(line) for line in visible]

    def _holds(self, words: Sequence[str], consumed: int) -> bool:
        """Report whether the frozen lines still match this transcript."""
        if len(words) < consumed:
            return False
        if not self._locked:
            return consumed == 0
        anchor = self._locked[-1]
        return list(words[consumed - len(anchor) : consumed]) == anchor


def wrap_words(
    words: Sequence[str], measure: Any, max_width: int
) -> list[list[str]]:
    """Greedily word-wrap into lines, each returned as its own word list."""
    lines: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if current and measure(" ".join((*current, word))) > max_width:
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)
    return lines


def _line_width(font: Any, screen_width: int) -> int:
    """Size a caption line in characters, the way a caption track reads."""
    average = font.measure(CAPTION_WIDTH_SAMPLE) / len(CAPTION_WIDTH_SAMPLE)
    return max(
        320,
        min(
            int(average * CAPTION_LINE_CHARACTERS),
            int(screen_width * CAPTION_WIDTH_FRACTION),
        ),
    )


def _caption_family(tkfont: Any) -> str:
    """Pick the closest available face to YouTube's proportional sans-serif."""
    available = {name.lower() for name in tkfont.families()}
    for family in CAPTION_FONT_CANDIDATES:
        if family.lower() in available:
            return family
    return "TkDefaultFont"


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

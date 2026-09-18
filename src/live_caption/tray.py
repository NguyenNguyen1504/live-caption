"""Minimal native Windows notification-area icon.

This avoids adding an image toolkit solely for a four-item tray menu. All Win32
work stays on its own message-loop thread; commands are handed back to Tk
through a thread-safe callback.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Callable

WM_APP = 0x8000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_DISABLED = 0x00000002
MF_GRAYED = 0x00000001
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

CMD_TOGGLE = 1001
CMD_DEMO = 1002
CMD_REAL = 1003
CMD_CONFIGURE = 1004
CMD_EXIT = 1005

CALLBACK_MESSAGE = WM_APP + 1
UPDATE_MESSAGE = WM_APP + 2


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


if sys.platform == "win32":
    WNDPROC = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]


class WindowsTray:
    """A notification-area icon with the application's complete control set."""

    def __init__(self, on_command: Callable[[str], None]) -> None:
        self.on_command = on_command
        self.running = False
        self.mode = "demo"
        self.hwnd = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failed = False
        self._wndproc_callback: object | None = None
        self._icon = 0

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(
            target=self._message_loop, name="caption-tray", daemon=True
        )
        self._thread.start()
        self._ready.wait(3)
        return not self._failed and bool(self.hwnd)

    def update(self, *, running: bool, mode: str) -> None:
        self.running = running
        self.mode = mode
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, UPDATE_MESSAGE, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def _message_loop(self) -> None:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32
            self._configure_functions(user32, kernel32, shell32)
            instance = kernel32.GetModuleHandleW(None)
            class_name = f"LocalDictationCaptionTray-{id(self)}"

            @WNDPROC  # type: ignore[name-defined]
            def window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
                if message == CALLBACK_MESSAGE:
                    event = int(lparam) & 0xFFFF
                    if event in {WM_RBUTTONUP, WM_CONTEXTMENU}:
                        self._show_menu()
                    elif event == WM_LBUTTONDBLCLK:
                        self.on_command("toggle")
                    return 0
                if message == UPDATE_MESSAGE:
                    self._write_icon(NIM_MODIFY)
                    return 0
                if message == WM_CLOSE:
                    self._write_icon(NIM_DELETE)
                    user32.DestroyWindow(hwnd)
                    return 0
                if message == WM_DESTROY:
                    self.hwnd = 0
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self._wndproc_callback = window_proc
            window_class = WNDCLASSW()  # type: ignore[name-defined]
            window_class.lpfnWndProc = window_proc
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError()
            user32.CreateWindowExW.restype = wintypes.HWND
            self.hwnd = user32.CreateWindowExW(
                0, class_name, class_name, 0, 0, 0, 0, 0, None, None, instance, None
            )
            if not self.hwnd:
                raise ctypes.WinError()
            user32.LoadIconW.restype = wintypes.HANDLE
            icon_name = ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR)
            self._icon = user32.LoadIconW(None, icon_name)
            self._write_icon(NIM_ADD)
            data = self._icon_data()
            data.uVersion = NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception:  # noqa: BLE001 - Win32 initialization failures vary
            self._failed = True
            self._ready.set()

    @staticmethod
    def _configure_functions(user32: object, kernel32: object, shell32: object) -> None:
        """Declare pointer-sized signatures explicitly for 64-bit Windows."""
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]  # type: ignore[attr-defined]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE  # type: ignore[attr-defined]
        user32.RegisterClassW.argtypes = [  # type: ignore[attr-defined,name-defined]
            ctypes.POINTER(WNDCLASSW)
        ]
        user32.RegisterClassW.restype = wintypes.WORD  # type: ignore[attr-defined]
        user32.CreateWindowExW.argtypes = [  # type: ignore[attr-defined]
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND  # type: ignore[attr-defined]
        user32.DefWindowProcW.argtypes = [  # type: ignore[attr-defined]
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t  # type: ignore[attr-defined]
        user32.DestroyWindow.argtypes = [wintypes.HWND]  # type: ignore[attr-defined]
        user32.DestroyWindow.restype = wintypes.BOOL  # type: ignore[attr-defined]
        user32.PostMessageW.argtypes = [  # type: ignore[attr-defined]
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL  # type: ignore[attr-defined]
        user32.GetMessageW.argtypes = [  # type: ignore[attr-defined]
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = wintypes.BOOL  # type: ignore[attr-defined]
        user32.TranslateMessage.argtypes = [  # type: ignore[attr-defined]
            ctypes.POINTER(wintypes.MSG)
        ]
        user32.DispatchMessageW.argtypes = [  # type: ignore[attr-defined]
            ctypes.POINTER(wintypes.MSG)
        ]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t  # type: ignore[attr-defined]
        user32.LoadIconW.argtypes = [  # type: ignore[attr-defined]
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
        ]
        user32.LoadIconW.restype = wintypes.HANDLE  # type: ignore[attr-defined]
        user32.CreatePopupMenu.restype = wintypes.HMENU  # type: ignore[attr-defined]
        user32.AppendMenuW.argtypes = [  # type: ignore[attr-defined]
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        user32.TrackPopupMenu.argtypes = [  # type: ignore[attr-defined]
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        user32.TrackPopupMenu.restype = wintypes.UINT  # type: ignore[attr-defined]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]  # type: ignore[attr-defined]
        user32.GetCursorPos.argtypes = [  # type: ignore[attr-defined]
            ctypes.POINTER(wintypes.POINT)
        ]
        user32.GetCursorPos.restype = wintypes.BOOL  # type: ignore[attr-defined]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]  # type: ignore[attr-defined]
        user32.SetForegroundWindow.restype = wintypes.BOOL  # type: ignore[attr-defined]
        shell32.Shell_NotifyIconW.argtypes = [  # type: ignore[attr-defined]
            wintypes.DWORD,
            ctypes.POINTER(NOTIFYICONDATAW),
        ]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL  # type: ignore[attr-defined]

    def _icon_data(self) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = CALLBACK_MESSAGE
        data.hIcon = self._icon
        state = "running" if self.running else "stopped"
        mode = "Demo" if self.mode == "demo" else "Real API"
        data.szTip = f"Local Dictation Captions — {mode}, {state}"[:127]
        return data

    def _write_icon(self, action: int) -> None:
        ctypes.windll.shell32.Shell_NotifyIconW(action, ctypes.byref(self._icon_data()))

    def _show_menu(self) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            toggle_text = "Stop captions" if self.running else "Start captions"
            user32.AppendMenuW(menu, MF_STRING, CMD_TOGGLE, toggle_text)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if self.mode == "demo" else 0),
                CMD_DEMO,
                "Demo Mode",
            )
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if self.mode == "real" else 0),
                CMD_REAL,
                "Real API Mode",
            )
            user32.AppendMenuW(menu, MF_STRING, CMD_CONFIGURE, "Configure Real API…")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "Exit")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)
            command = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                self.hwnd,
                None,
            )
            mapping = {
                CMD_TOGGLE: "toggle",
                CMD_DEMO: "demo",
                CMD_REAL: "real",
                CMD_CONFIGURE: "configure",
                CMD_EXIT: "exit",
            }
            if command in mapping:
                self.on_command(mapping[command])
        finally:
            user32.DestroyMenu(menu)

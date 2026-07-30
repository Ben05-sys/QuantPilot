"""System tray icon and a global summon hotkey — pure Win32 via ctypes.

Ported from `OfflinePilotX/app/tray.py`, which has been running reliably
for months. The differences are the class name (two tray windows cannot
share one), the hotkey (Ctrl+Alt+M for markets, so both apps can be
resident at once), and the icon.

Everything here is best-effort. A tray icon that fails to appear is a
cosmetic problem; it must never take the server down with it, so every
entry point is wrapped and the thread is a daemon.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path

WM_DESTROY = 0x0002
WM_HOTKEY = 0x0312
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP_TRAY = 0x8001
NIM_ADD, NIM_DELETE = 0x0, 0x2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MOD_ALT, MOD_CONTROL = 0x1, 0x2
VK_M, VK_K = 0x4D, 0x4B
TPM_RETURNCMD = 0x0100
CMD_OPEN, CMD_REFRESH, CMD_QUIT = 1, 2, 3

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

# Without explicit argtypes, ctypes marshals Python ints as 32-bit C ints,
# and any LPARAM above 2^31 raises "int too long to convert" inside the
# window procedure. The exception is swallowed by the callback boundary,
# so the symptom is a tray icon that renders but ignores every click while
# stderr fills with tracebacks.
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.restype = wintypes.HICON
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.TrackPopupMenu.restype = ctypes.c_int


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def icon_path() -> Path | None:
    base = (Path(getattr(sys, "_MEIPASS", ""))
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1])
    ico = base / "assets" / "pilotmarkets.ico"
    return ico if ico.is_file() else None


def start_tray(url: str, state=None) -> None:
    threading.Thread(target=_run, args=(url, state), daemon=True,
                     name="pm-tray").start()


def _run(url: str, state=None) -> None:
    from .server import open_window

    def open_app():
        try:
            open_window(url)
        except Exception:  # noqa: BLE001
            pass

    def refresh():
        try:
            if state is not None:
                state.start_refresh(note="tray")
        except Exception:  # noqa: BLE001
            pass

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            event = lparam & 0xFFFF
            if event == WM_LBUTTONDBLCLK:
                open_app()
            elif event == WM_RBUTTONUP:
                menu = user32.CreatePopupMenu()
                user32.AppendMenuW(menu, 0, CMD_OPEN, "Open PilotMarkets")
                user32.AppendMenuW(menu, 0, CMD_REFRESH, "Refresh universe")
                user32.AppendMenuW(menu, 0x800, 0, None)  # separator
                user32.AppendMenuW(menu, 0, CMD_QUIT, "Quit")
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                user32.SetForegroundWindow(hwnd)
                cmd = user32.TrackPopupMenu(
                    menu, TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
                user32.DestroyMenu(menu)
                if cmd == CMD_OPEN:
                    open_app()
                elif cmd == CMD_REFRESH:
                    refresh()
                elif cmd == CMD_QUIT:
                    _remove_icon(hwnd)
                    os._exit(0)
            return 0
        if msg == WM_HOTKEY:
            open_app()
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wnd_proc)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASS()
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    # Distinct from OfflinePilotX's class so both can sit in the tray.
    wc.lpszClassName = "PilotMarketsTray"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        return
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "PilotMarkets",
                                  0, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        return

    hicon = None
    ico = icon_path()
    if ico:
        hicon = user32.LoadImageW(None, str(ico), 1, 16, 16, 0x10)  # ICON,file
    if not hicon:
        hicon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION fallback

    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_APP_TRAY
    nid.hIcon = hicon
    nid.szTip = "PilotMarkets — Ctrl+Alt+M to summon"
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    # Ctrl+Alt+M, falling back to Ctrl+Alt+K if something else owns it.
    if not user32.RegisterHotKey(hwnd, 1, MOD_CONTROL | MOD_ALT, VK_M):
        user32.RegisterHotKey(hwnd, 1, MOD_CONTROL | MOD_ALT, VK_K)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    _remove_icon(hwnd)


def _remove_icon(hwnd) -> None:
    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

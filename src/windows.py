"""win32 window enumeration, focus and native window helpers.

Contract: plugins/super-cu/CONTRACT.md §3.1.  Pure ctypes / win32, no UIA
enumeration of the desktop (RootElement.GetChildren() costs ~420ms and is
forbidden on hot paths).  Window list comes from EnumWindows (~1ms).

All coordinates returned here are **physical screen pixels**: this module sets
per-monitor DPI awareness at import time, so GetWindowRect / UIA rects /
ImageGrab / SetCursorPos all speak the same coordinate system.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

__all__ = [
    "screen_size",
    "virtual_screen",
    "list_windows",
    "foreground_hwnd",
    "find_windows",
    "resolve_hwnd",
    "activate",
    "activate_ex",
    "get_rect",
    "get_title",
    "window_info",
    "wait_for_window",
    "proc_name",
    "close_window",
    "show_window",
    "is_minimized",
    "is_maximized",
    "is_window",
    "WindowGone",
    "ClipboardBusy",
    "LAST_CLIPBOARD_ATTEMPTS",
    "set_clipboard_text",
    "get_clipboard_text",
    "clipboard_sequence",
]


# --------------------------------------------------------------------------- #
# DPI awareness -- must happen before any window / DC / UIA object is created.
# Without it (python.exe is DPI-unaware by default on a 150%-scaled 2560x1600
# screen) win32 reports a virtualised 1707x1067 while ImageGrab returns
# 2560x1600, and every click coordinate would be off by 1.5x.
# --------------------------------------------------------------------------- #
def _set_dpi_aware() -> dict:
    """Delegate to the Lead-owned src/dpi.py; fall back to a local attempt."""
    if sys.platform != "win32":
        return {"ok": False, "method": "non-win32"}
    state = None
    try:
        from dpi import ensure_dpi_aware  # type: ignore  (Lead-owned module)

        state = ensure_dpi_aware()
    except Exception:
        try:  # imported as a package (plugins.super_cu...)
            from .dpi import ensure_dpi_aware  # type: ignore

            state = ensure_dpi_aware()
        except Exception:
            state = None
    if isinstance(state, dict) and state.get("ok"):
        return state
    try:  # per-monitor v2 (Win10 1703+)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return {"ok": True, "method": "SetProcessDpiAwarenessContext(PMv2)"}
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
        return {"ok": True, "method": "shcore.SetProcessDpiAwareness(2)"}
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return {"ok": True, "method": "SetProcessDPIAware()"}
    except Exception:
        return {"ok": False, "method": "failed"}


DPI_STATE: dict = _set_dpi_aware()

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

try:
    _dwmapi = ctypes.windll.dwmapi
except Exception:  # pragma: no cover
    _dwmapi = None

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
def screen_size() -> tuple[int, int]:
    """Primary screen size in physical pixels."""
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the virtual desktop in physical pixels (may be negative)."""
    return (
        int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
        int(user32.GetSystemMetrics(77)),
        int(user32.GetSystemMetrics(78)),
        int(user32.GetSystemMetrics(79)),
    )


# --------------------------------------------------------------------------- #
# process names (cached)
# --------------------------------------------------------------------------- #
_proc_cache: dict[int, str] = {}
_proc_lock = threading.Lock()

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


def proc_name(pid: int) -> str:
    """Executable name without '.exe' (contract example: 'msedge')."""
    if not pid:
        return ""
    with _proc_lock:
        hit = _proc_cache.get(pid)
    if hit is not None:
        return hit
    name = ""
    if psutil is not None:
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = ""
    if not name:
        try:
            h = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wt.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        name = os.path.basename(buf.value)
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            name = ""
    if name.lower().endswith(".exe"):
        name = name[:-4]
    with _proc_lock:
        _proc_cache[pid] = name
    return name


# --------------------------------------------------------------------------- #
# per-window primitives
# --------------------------------------------------------------------------- #
def get_title(hwnd: int) -> str:
    hwnd = int(hwnd)
    if not hwnd:
        return ""
    n = int(user32.GetWindowTextLengthW(wt.HWND(hwnd)))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, n + 2)
    return buf.value


def get_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(int(hwnd)), buf, 256)
    return buf.value


def get_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(wt.HWND(int(hwnd)), ctypes.byref(pid))
    return int(pid.value)


def _dwm_cloaked(hwnd: int) -> bool:
    if _dwmapi is None:
        return False
    try:
        val = wt.DWORD(0)
        hr = _dwmapi.DwmGetWindowAttribute(
            wt.HWND(int(hwnd)), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)
        )
        return hr == 0 and val.value != 0
    except Exception:
        return False


def get_rect(hwnd: int) -> list[int]:
    """[l, t, r, b] in physical pixels, DWM extended frame bounds when known."""
    hwnd = int(hwnd)
    if _dwmapi is not None:
        try:
            r = wt.RECT()
            hr = _dwmapi.DwmGetWindowAttribute(
                wt.HWND(hwnd),
                DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(r),
                ctypes.sizeof(r),
            )
            if hr == 0 and r.right > r.left and r.bottom > r.top:
                return [int(r.left), int(r.top), int(r.right), int(r.bottom)]
        except Exception:
            pass
    r = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        return [0, 0, 0, 0]
    return [int(r.left), int(r.top), int(r.right), int(r.bottom)]


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(wt.HWND(int(hwnd))))


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(wt.HWND(int(hwnd))))


def is_maximized(hwnd: int) -> bool:
    """True for a maximized (zoomed) window.

    A maximized window must be restored before a caption drag: otherwise
    Windows' own restore/snap handling moves it in a way that does not follow
    the cursor (the "drag only moved vertically" symptom).
    """
    try:
        return bool(user32.IsZoomed(wt.HWND(int(hwnd))))
    except Exception:
        return False


def _is_tool_window(hwnd: int) -> bool:
    try:
        ex = int(user32.GetWindowLongW(wt.HWND(int(hwnd)), GWL_EXSTYLE))
    except Exception:
        return False
    ex &= 0xFFFFFFFF
    return bool(ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW)


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def _rect_area(rect: list[int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _info(hwnd: int, z: int = 0, fg: int = 0) -> dict:
    hwnd = int(hwnd)
    rect = get_rect(hwnd)
    pid = get_pid(hwnd)
    return {
        "hwnd": hwnd,
        "pid": pid,
        "proc": proc_name(pid),
        "title": get_title(hwnd),
        "class": get_class(hwnd),
        "rect": rect,
        "visible": is_visible(hwnd),
        "minimized": is_minimized(hwnd),
        "focused": hwnd == fg,
        "z": z,
    }


# --------------------------------------------------------------------------- #
# enumeration
# --------------------------------------------------------------------------- #
def list_windows(*, include_invisible: bool = False, limit: int = 0) -> list[dict]:
    """Top-level windows in z-order (topmost first).

    Pure EnumWindows (~1ms for ~640 handles); UIA RootElement is never touched.
    Filters out tool windows, cloaked UWP ghosts and zero-area shells.
    """
    handles: list[int] = []

    def _cb(hwnd, _lparam):
        handles.append(int(hwnd))
        return True

    proc = _EnumWindowsProc(_cb)
    user32.EnumWindows(proc, 0)
    fg = foreground_hwnd()

    out: list[dict] = []
    for h in handles:
        is_fg = h == fg
        if not include_invisible and not is_visible(h):
            continue
        rect = get_rect(h)
        if not include_invisible and not is_fg:
            # The foreground window is always listed even if it is a tool
            # window / untitled popup: callers use focused==True to find it.
            if _dwm_cloaked(h):
                continue
            if _is_tool_window(h):
                continue
            if _rect_area(rect) <= 0:
                continue
            if not get_title(h) and _rect_area(rect) < 20000:
                continue
        pid = get_pid(h)
        out.append(
            {
                "hwnd": h,
                "pid": pid,
                "proc": proc_name(pid),
                "title": get_title(h),
                "class": get_class(h),
                "rect": rect,
                "visible": is_visible(h),
                "minimized": is_minimized(h),
                "focused": h == fg,
                "z": len(out),
            }
        )
        if limit and len(out) >= limit:
            break
    return out


def window_info(hwnd: int) -> dict:
    """Full info dict for one window (used by bridge.snapshot)."""
    hwnd = int(hwnd)
    if not hwnd or not user32.IsWindow(wt.HWND(hwnd)):
        raise WindowGone(f"窗口 #{hwnd} 已不存在（可能已关闭或被系统重建）")
    fg = foreground_hwnd()
    pid = get_pid(hwnd)
    return {
        "hwnd": hwnd,
        "pid": pid,
        "proc": proc_name(pid),
        "title": get_title(hwnd),
        "class": get_class(hwnd),
        "rect": get_rect(hwnd),
        "visible": is_visible(hwnd),
        "minimized": is_minimized(hwnd),
        "focused": hwnd == fg,
        "z": 0,
    }


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad"), None)


def _norm(s: str) -> str:
    """Lowercase, drop whitespace + zero-width chars + soft hyphen."""
    if not s:
        return ""
    s = s.translate(_ZERO_WIDTH)
    return "".join(ch for ch in s.lower() if not ch.isspace())


def find_windows(query: str, limit: int = 10) -> list[dict]:
    """Match by '#hwnd' / title substring / process name / class name.

    Ranking: exact title > prefix > substring, boosted by focused / visible / area.
    """
    q = (query or "").strip()
    if not q:
        return []
    if q.startswith("#"):
        try:
            h = int(q[1:], 16) if q[1:].lower().startswith("0x") else int(q[1:])
        except ValueError:
            return []
        if not user32.IsWindow(wt.HWND(h)):
            return []
        return [window_info(h)]

    wins = list_windows()
    nq = _norm(q)
    scored: list[tuple[float, dict]] = []
    for w in wins:
        title = _norm(w["title"])
        prc = _norm(w["proc"])
        cls = _norm(w["class"])
        score = 0.0
        if title:
            if title == nq:
                score = 100.0
            elif title.startswith(nq):
                score = 80.0
            elif nq in title:
                score = 60.0
        if prc:
            if prc == nq:
                score = max(score, 90.0)
            elif nq in prc:
                score = max(score, 50.0)
        if cls and nq in cls:
            score = max(score, 40.0)
        if score <= 0:
            continue
        if w["focused"]:
            score += 12.0
        if w["visible"]:
            score += 3.0
        if not w["minimized"]:
            score += 2.0
        score += min(_rect_area(w["rect"]) / 1e7, 1.0)
        scored.append((score, w))
    scored.sort(key=lambda t: -t[0])
    return [w for _s, w in scored[: max(1, int(limit))]]


def resolve_hwnd(query: str | None) -> int | None:
    """None / '' => foreground window; else best match from find_windows."""
    if query is None or not str(query).strip():
        h = foreground_hwnd()
        return h or None
    hits = find_windows(str(query), limit=1)
    return hits[0]["hwnd"] if hits else None


# --------------------------------------------------------------------------- #
# focus / window state
# --------------------------------------------------------------------------- #
class WindowGone(RuntimeError):
    """The window handle is no longer a window (closed / re-created)."""


def is_window(hwnd: int) -> bool:
    try:
        return bool(user32.IsWindow(wt.HWND(int(hwnd))))
    except Exception:
        return False


def _wait_foreground(hwnd: int, timeout_ms: int) -> bool:
    h = int(hwnd)
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
    while time.monotonic() < deadline:
        fg = foreground_hwnd()
        if fg == h:
            return True
        if fg and int(user32.GetAncestor(wt.HWND(fg), 2)) == h:
            return True
        time.sleep(0.008)
    fg = foreground_hwnd()
    return fg == h or bool(fg) and int(user32.GetAncestor(wt.HWND(fg), 2)) == h


def _tap_alt() -> None:
    """Press/release ALT: the documented way to unlock SetForegroundWindow."""
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    try:
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _activate_core(h: int, wait_ms: int = 120) -> bool:
    """Foreground escalation chain (assumes the window is already shown)."""
    if foreground_hwnd() == h:
        if wait_ms:
            time.sleep(min(int(wait_ms), 40) / 1000.0)
        return True

    # 1) plain attempt
    try:
        user32.SetForegroundWindow(wt.HWND(h))
    except Exception:
        pass
    if _wait_foreground(h, 120):
        return True

    # 2) ALT unlock, then retry (no thread attach involved)
    _tap_alt()
    try:
        user32.SetForegroundWindow(wt.HWND(h))
    except Exception:
        pass
    if _wait_foreground(h, 150):
        return True

    # 3) SwitchToThisWindow (reaches windows that ignore SetForegroundWindow)
    try:
        user32.SwitchToThisWindow(wt.HWND(h), True)
    except Exception:
        pass
    if _wait_foreground(h, 150):
        return True

    # 4) last resort: attach only to the *current foreground* thread
    fg = foreground_hwnd()
    cur = int(kernel32.GetCurrentThreadId())
    fg_thread = int(user32.GetWindowThreadProcessId(wt.HWND(fg), None)) if fg else 0
    if fg_thread and fg_thread != cur:
        attached = False
        try:
            attached = bool(user32.AttachThreadInput(wt.DWORD(cur), wt.DWORD(fg_thread), True))
            user32.BringWindowToTop(wt.HWND(h))
            user32.SetForegroundWindow(wt.HWND(h))
        except Exception:
            pass
        finally:
            if attached:
                try:
                    user32.AttachThreadInput(wt.DWORD(cur), wt.DWORD(fg_thread), False)
                except Exception:
                    pass
    return _wait_foreground(h, max(int(wait_ms), 150))


def activate_ex(hwnd: int, wait_ms: int = 120) -> dict:
    """Bring a window to the foreground and say **why** if that failed.

    Handles the two states that used to fail silently:
      * minimized (``IsIconic``): restored first — SetForegroundWindow alone
        never un-minimizes a window;
      * hidden / tray (``IsWindowVisible`` false, e.g. WeChat in the tray):
        shown first, otherwise no foreground API can reach it.
    """
    h = int(hwnd)
    state: dict = {
        "ok": False,
        "was_minimized": False,
        "was_hidden": False,
        "restored": False,
        "reason": "",
    }
    if not h or not user32.IsWindow(wt.HWND(h)):
        state["reason"] = "窗口已关闭（hwnd 不存在）"
        return state
    state["was_minimized"] = is_minimized(h)
    state["was_hidden"] = not is_visible(h)
    if state["was_hidden"]:
        user32.ShowWindow(wt.HWND(h), SW_SHOW)
        time.sleep(0.10)
    if state["was_minimized"]:
        for _ in range(3):
            user32.ShowWindow(wt.HWND(h), SW_RESTORE)
            time.sleep(0.10)
            if not is_minimized(h):
                break
        state["restored"] = not is_minimized(h)
    elif not state["was_hidden"]:
        state["restored"] = True
    if not is_visible(h):
        user32.ShowWindow(wt.HWND(h), SW_SHOW)
        time.sleep(0.08)

    state["ok"] = _activate_core(h, wait_ms)
    if not state["ok"]:
        if is_minimized(h):
            state["reason"] = "窗口仍处于最小化状态，无法还原（SW_RESTORE 重试 3 次失败）"
        elif not is_visible(h):
            state["reason"] = "窗口处于隐藏状态（可能在托盘里），无法显示"
        else:
            state["reason"] = (
                "窗口已显示，但前台锁被其他进程占用："
                "SetForegroundWindow / ALT 解锁 / SwitchToThisWindow / AttachThreadInput 均未能拿到前台"
            )
    return state


def activate(hwnd: int, wait_ms: int = 120) -> bool:
    """Bring a window to the foreground (restores minimized / reveals hidden)."""
    return bool(activate_ex(hwnd, wait_ms).get("ok"))


def show_window(hwnd: int, cmd: int) -> bool:
    return bool(user32.ShowWindow(wt.HWND(int(hwnd)), int(cmd)))


def close_window(hwnd: int) -> bool:
    WM_CLOSE = 0x0010
    return bool(user32.PostMessageW(wt.HWND(int(hwnd)), WM_CLOSE, 0, 0))


def wait_for_window(query: str, timeout_ms: int = 3000, interval_ms: int = 60) -> dict | None:
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
    while True:
        hits = find_windows(query, limit=1)
        if hits:
            return hits[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(20, int(interval_ms)) / 1000.0)


def screen_snapshot_map(
    origin: list[int], img_w: int, img_h: int, screen_w: int, screen_h: int
) -> dict:
    """Small helper used by bridge.screenshot / last_shot_map."""
    scale = (screen_w / img_w) if img_w else 1.0
    return {
        "origin": [int(origin[0]), int(origin[1])],
        "scale": scale,
        "img_w": int(img_w),
        "img_h": int(img_h),
        "screen_w": int(screen_w),
        "screen_h": int(screen_h),
    }


# --------------------------------------------------------------------------- #
# clipboard -- own implementation, because uiautomation's SetClipboardText
# swallows OpenClipboard failures (that made `paste` silently paste the user's
# PREVIOUS clipboard content, e.g. somebody else's URL).
# --------------------------------------------------------------------------- #
CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_GMEM_ZEROINIT = 0x0040

# 64-bit correctness: HGLOBAL / HLOCAL are pointers.  Without explicit
# restypes ctypes truncates them to int and every clipboard write fails.
try:
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wt.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = wt.BOOL
    user32.EmptyClipboard.restype = wt.BOOL
    user32.CloseClipboard.restype = wt.BOOL
    user32.GetClipboardData.argtypes = [wt.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
except Exception:  # pragma: no cover
    pass


class ClipboardBusy(RuntimeError):
    """OpenClipboard kept failing: another program owns the clipboard."""


#: Attempts used by the last set_clipboard_text() call (1 = first try worked).
LAST_CLIPBOARD_ATTEMPTS = 0


def _open_clipboard(attempts: int = 6, backoff=None) -> int:
    """Retry OpenClipboard with 30-120ms backoff; returns attempts used (0=ok)."""
    for i in range(max(1, int(attempts))):
        try:
            if user32.OpenClipboard(None):
                return i
        except Exception:
            pass
        time.sleep(min(0.12, 0.03 * (i + 1)))
    return -1


def get_clipboard_text(attempts: int = 6) -> str | None:
    """Read CF_UNICODETEXT, retrying while another app owns the clipboard."""
    used = _open_clipboard(attempts)
    if used < 0:
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        addr = kernel32.GlobalLock(h)
        if not addr:
            return None
        try:
            return ctypes.wstring_at(addr)
        finally:
            kernel32.GlobalUnlock(h)
    except Exception:
        return None
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


def set_clipboard_text(text: str, attempts: int = 6) -> bool:
    """Set CF_UNICODETEXT reliably; False means "did not write", never silent.

    Every failure path closes the clipboard and backs off 30-120ms before the
    next attempt (WeChat / IME helpers hold the clipboard for short bursts).
    """
    text = text or ""
    raw = text.encode("utf-16-le")
    global LAST_CLIPBOARD_ATTEMPTS
    tries = max(1, int(attempts))
    for i in range(tries):
        LAST_CLIPBOARD_ATTEMPTS = i + 1
        if not user32.OpenClipboard(None):
            time.sleep(min(0.12, 0.03 * (i + 1)))
            continue
        ok = False
        try:
            if not user32.EmptyClipboard():
                time.sleep(min(0.12, 0.03 * (i + 1)))
                continue
            size = len(raw) + 2  # include the UTF-16 NUL terminator
            h = kernel32.GlobalAlloc(_GMEM_MOVEABLE | _GMEM_ZEROINIT, size)
            if h:
                addr = kernel32.GlobalLock(h)
                if addr:
                    try:
                        if raw:
                            ctypes.memmove(addr, raw, len(raw))
                    finally:
                        kernel32.GlobalUnlock(h)
                    if user32.SetClipboardData(CF_UNICODETEXT, h):
                        ok = True  # system owns h now
                    else:
                        kernel32.GlobalFree(h)
                else:
                    kernel32.GlobalFree(h)
            if ok:
                return True
        except Exception:
            ok = False
        finally:
            try:
                user32.CloseClipboard()
            except Exception:
                pass
        time.sleep(min(0.12, 0.03 * (i + 1)))
    return False


def clipboard_sequence() -> int:
    """GetClipboardSequenceNumber: changes whenever the clipboard is rewritten."""
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


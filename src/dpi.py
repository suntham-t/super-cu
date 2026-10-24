"""Claim a single, physical-pixel coordinate space for the whole process.

Why this file exists
--------------------
On a 150%-scaled display a DPI-unaware python process lives in a virtualized
logical space (GetSystemMetrics -> 1707x1067 for a 2560x1600 panel) while
Pillow's ``ImageGrab`` hands back *physical* pixels (2560x1600).  UI Automation
rectangles, ``GetWindowRect`` and the cursor APIs follow the process' awareness,
so mixing them silently produces a 1.5x error: a click derived from a screenshot
lands far away from the element the model aimed at.

Declaring ``PER_MONITOR_AWARE_V2`` up front puts UI Automation rects,
``GetWindowRect``, ``SetCursorPos``/``SendInput`` and screen capture into ONE
physical-pixel space, which is also the space the existing ``windows-mcp``
server uses.  Call :func:`ensure_dpi_aware` before any UIA, win32 or PIL call;
it is idempotent and safe to call from several modules.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

_PER_MONITOR_AWARE_V2 = -4
_PROCESS_PER_MONITOR_DPI_AWARE = 2
_STATE: dict | None = None


def ensure_dpi_aware() -> dict:
    """Declare per-monitor-v2 DPI awareness once; return a diagnostic dict.

    Keys: ``awareness`` (raw OS value: 0 unaware, 1 system, 2 per-monitor),
    ``context`` (Win10 1703+ awareness context), ``method`` (how it was set),
    ``screen`` (GetSystemMetrics after the call), ``ok``.
    """
    global _STATE
    if _STATE is not None:
        return _STATE

    state: dict = {"ok": False, "method": None}
    user32 = ctypes.windll.user32

    # Win10 1703+: SetProcessDpiAwarenessContext.  Fails if awareness was
    # already fixed (manifest or an earlier call) — that is fine.
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wt.BOOL
        if fn(ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
            state["method"] = "SetProcessDpiAwarenessContext(PMv2)"
            state["ok"] = True
    except Exception as exc:  # pragma: no cover - depends on OS build
        state["ctx_err"] = str(exc)

    if not state["ok"]:
        try:
            shcore = ctypes.windll.shcore
            hr = shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
            state["method"] = f"shcore.SetProcessDpiAwareness({hr})"
            state["ok"] = hr in (0, 0x80070005)  # S_OK or E_ACCESSDENIED(already set)
        except Exception as exc:  # pragma: no cover
            state["shcore_err"] = str(exc)

    # Report the authoritative values, not our intent.
    try:
        awareness = ctypes.c_int(-1)
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness))
        state["awareness"] = awareness.value
    except Exception:
        state["awareness"] = None
    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        state["context"] = user32.GetAwarenessFromDpiAwarenessContext(ctx) if ctx else None
    except Exception:
        state["context"] = None

    state["screen"] = [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]
    state["virtual_screen"] = [
        user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
        user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
        user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
    ]
    _STATE = state
    return state


if __name__ == "__main__":
    import json

    print(json.dumps(ensure_dpi_aware(), ensure_ascii=False, indent=2))

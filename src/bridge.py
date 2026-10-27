"""super-cu UIA bridge -- snapshot, stable element ids, resolution, actions.

Contract: plugins/super-cu/CONTRACT.md §3.2 / §3.3.

Design rules that keep this layer fast and accurate:

* Window list never comes from UIA ``RootElement.GetChildren()`` (~420ms); it
  comes from :mod:`windows` (ctypes ``EnumWindows``, ~1ms).
* One window is walked with a depth-first pass that prunes subtrees that cannot
  hold interesting children (text/image/button/... are leaves).  Property reads
  are delayed until after ranking + ``max_nodes`` truncation, so a 400-node Edge
  window costs ~110ms walk + ~35ms type + ~25ms name + ~60ms props.
* Element ids are UIA ``RuntimeId`` -> process-local int, so the same element
  keeps the same id across snapshots.  Actions never blind-click a stale rect:
  a dead element is relocated by RuntimeId once, then we fail loudly.
* Every call is DPI-aware (``src/dpi.py``) so UIA rects, ``GetWindowRect``,
  ``ImageGrab`` and ``SendInput`` all speak physical pixels.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wt
import fnmatch
import io
import os
import random
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# windows.py declares DPI awareness at import time -- keep this import first.
try:  # imported as a package (plugins.super_cu...)
    from . import windows as _win
except ImportError:  # imported with src/ on sys.path
    import windows as _win

DPI_STATE = _win.DPI_STATE

try:
    from .gui_lock import GuiBusy, gui_locked
    from .gui_lock import LOCK_PATH as _LOCK_PATH
except ImportError:
    from gui_lock import GuiBusy, gui_locked
    from gui_lock import LOCK_PATH as _LOCK_PATH

import uiautomation as auto

__all__ = [
    "Bridge",
    "BridgeError",
    "AmbiguousError",
    "ClipboardBusy",
    "Element",
    "CT_SHORT",
    "LOCK_ACTIONS",
    "HOLD_MIN_MS",
    "HOLD_MAX_MS",
    "hold_keys",
    "hold_mouse",
    "collect_marks",
    "annotate_marks",
    "drag_path",
    "MAX_IDS",
    "MAX_WINDOWS",
]

#: Retention caps for a long-running server: every Element holds a COM
#: reference and every cached window holds a whole UIA element subtree, so both
#: maps are bounded (oldest entries drop first; ids are never reused).
MAX_IDS = 20000
MAX_WINDOWS = 32

#: uiautomation's global search timeout is applied once, from Bridge.__init__:
#: importing this module must stay free of COM / UIA global state so the MCP
#: server can cold-start lazily (see also the lazy PIL imports below).
_UIA_GLOBALS_READY = False


def _ensure_com() -> None:
    """Claim a COM apartment on the CURRENT thread before any UIA/COM call.

    comtypes runs CoInitializeEx() when *comtypes itself* is first imported, i.e.
    on whichever thread imported it first.  The MCP server prefetches bridge from
    a worker thread, so without this the main (request) thread would have no COM
    apartment and the first cu_show fails with:
        OSError: [WinError -2147221008] 尚未调用 CoInitialize。

    CoInitializeEx is per-thread and idempotent (S_FALSE when the thread already
    has an apartment); a host that already claimed another apartment model raises
    RPC_E_CHANGED_MODE, which is harmless here because COM is available anyway.
    """
    try:
        import comtypes

        comtypes.CoInitializeEx()
    except Exception:  # pragma: no cover - never fail to construct a Bridge
        pass


def _init_uia_globals() -> None:
    """Apply uiautomation process-wide settings (idempotent, no COM creation)."""
    global _UIA_GLOBALS_READY
    if _UIA_GLOBALS_READY:
        return
    _UIA_GLOBALS_READY = True
    try:
        auto.SetGlobalSearchTimeout(1.0)
    except Exception:  # pragma: no cover - never fail a snapshot over a setting
        pass


#: Actions that change focus / keyboard / mouse / window state: they must hold
#: the shared desktop lock (CONTRACT §2).  Pure UIA pattern actions (invoke /
#: toggle / select / expand / collapse / setvalue) run in the background and are
#: intentionally lock-free so they never queue behind another agent's GUI work.
LOCK_ACTIONS = frozenset(
    {
        "click",
        "dblclick",
        "rightclick",
        "hover",
        "focus",
        "type",
        "key",
        "hold",
        "hold_mouse",
        "scroll_to",
        "paste",
        "scroll",
        "drag",
        "activate",
        "launch",
        "close",
        "minimize",
        "maximize",
        "restore",
    }
)

#: Nesting depth of this process' hold on the desktop lock.
_GUI_DEPTH = 0
_GUI_DEPTH_LOCK = threading.Lock()

#: Actions on an element that need an IsEnabled pre-check.
_CLICKISH = frozenset(
    {"invoke", "toggle", "select", "click", "dblclick", "rightclick", "expand", "collapse", "hover"}
)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class BridgeError(RuntimeError):
    """Any actionable bridge failure; the message is shown to the model."""


class AmbiguousError(BridgeError):
    """A selector matched several elements; we refuse to guess."""

    def __init__(self, message: str, candidates: list[dict]):
        super().__init__(message)
        self.candidates = candidates


class ClipboardBusy(BridgeError, _win.ClipboardBusy):
    """剪贴板被其他程序占用（OpenClipboard 连续失败），且没有可用兜底路径。"""


# --------------------------------------------------------------------------- #
# control type tables
# --------------------------------------------------------------------------- #
CT_SHORT: dict[str, str] = {
    "ButtonControl": "button",
    "EditControl": "edit",
    "MenuItemControl": "menuitem",
    "TabItemControl": "tabitem",
    "ListItemControl": "listitem",
    "TreeItemControl": "treeitem",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radiobutton",
    "ComboBoxControl": "combobox",
    "HyperlinkControl": "hyperlink",
    "TextControl": "text",
    "DocumentControl": "document",
    "PaneControl": "pane",
    "WindowControl": "window",
    "ImageControl": "image",
    "MenuBarControl": "menubar",
    "ToolBarControl": "toolbar",
    "TabControl": "tab",
    "GroupControl": "group",
    "ScrollBarControl": "scrollbar",
    "SliderControl": "slider",
    "SpinnerControl": "spinner",
    "SplitButtonControl": "splitbutton",
    "TreeControl": "tree",
    "ListControl": "list",
    "TableControl": "table",
    "DataGridControl": "datagrid",
    "MenuControl": "menu",
    "ToolTipControl": "tooltip",
    "HeaderControl": "header",
    "HeaderItemControl": "headeritem",
    "CustomControl": "custom",
    "StatusBarControl": "statusbar",
    "ProgressBarControl": "progressbar",
    "SeparatorControl": "separator",
    "TitleBarControl": "titlebar",
    "AppBarControl": "appbar",
    "CalendarControl": "calendar",
    "ThumbControl": "thumb",
    "SemanticZoomControl": "semanticzoom",
}
SHORT_CT = {v: k for k, v in CT_SHORT.items()}

#: types whose children are noise for our purposes -- never descend.
LEAF_TYPES = frozenset(
    {
        "TextControl",
        "ImageControl",
        "ButtonControl",
        "HyperlinkControl",
        "CheckBoxControl",
        "RadioButtonControl",
        "EditControl",
        "SliderControl",
        "ScrollBarControl",
        "MenuItemControl",
        "TabItemControl",
        "ListItemControl",
        "SplitButtonControl",
        "ThumbControl",
        "ProgressBarControl",
        "SeparatorControl",
        "HeaderItemControl",
    }
)

INTERACTIVE_TYPES = frozenset(
    {
        "ButtonControl",
        "EditControl",
        "MenuItemControl",
        "TabItemControl",
        "ListItemControl",
        "TreeItemControl",
        "CheckBoxControl",
        "RadioButtonControl",
        "ComboBoxControl",
        "HyperlinkControl",
        "SliderControl",
        "SpinnerControl",
        "SplitButtonControl",
        "DocumentControl",
    }
)

#: 可交互类型的**短名**（Set-of-Mark 用；document 是整页内容，框住没意义）
#: 出现在标题栏判定点下方即说明"这是内容"，不是窗口标题栏
_CAPTION_BLOCK_CTS = frozenset(
    {
        "DocumentControl",
        "EditControl",
        "ListItemControl",
        "TreeItemControl",
        "TableControl",
        "DataGridControl",
        "TabItemControl",
        "TabControl",
        "ToolBarControl",
        "MenuBarControl",
        "ButtonControl",
        "HyperlinkControl",
        "SliderControl",
        "ComboBoxControl",
        "CheckBoxControl",
        "RadioButtonControl",
        "ScrollBarControl",
    }
)

MARK_CTS = frozenset(
    CT_SHORT[name] for name in INTERACTIVE_TYPES if name != "DocumentControl"
)

TEXT_TYPES = frozenset({"TextControl", "DocumentControl"})

CONTAINER_TYPES = frozenset(
    {
        "PaneControl",
        "GroupControl",
        "CustomControl",
        "ListControl",
        "TreeControl",
        "TableControl",
        "DataGridControl",
        "ToolBarControl",
        "MenuBarControl",
        "TabControl",
        "WindowControl",
        "MenuControl",
        "ToolTipControl",
    }
)

#: Static pattern hints per short type.  Actions below always probe the real
#: pattern before using it; this table is only the model-facing hint and is
#: derived from type instead of 7 COM round-trips per node.
PATTERN_HINTS: dict[str, list[str]] = {
    "button": ["invoke"],
    "splitbutton": ["invoke", "expandcollapse"],
    "menuitem": ["invoke"],
    "hyperlink": ["invoke"],
    "edit": ["setvalue", "text"],
    "document": ["text", "scroll"],
    "checkbox": ["toggle"],
    "radiobutton": ["selectionitem"],
    "tabitem": ["selectionitem"],
    "listitem": ["selectionitem"],
    "treeitem": ["selectionitem", "expandcollapse"],
    "combobox": ["expandcollapse", "selection"],
    "menu": ["expandcollapse"],
    "tab": ["selection"],
    "list": ["selection", "scroll"],
    "tree": ["selection", "scroll"],
    "slider": ["rangevalue"],
    "scrollbar": ["rangevalue"],
    "spinner": ["rangevalue"],
}


# --------------------------------------------------------------------------- #
# low level input synthesis (physical pixel space, thanks to dpi.py)
# --------------------------------------------------------------------------- #
_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_LLKHF = 0

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B

_VK: dict[str, int] = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "back": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "menu": 0x5D,
    "apps": 0x5D,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "win": VK_LWIN,
    "lwin": VK_LWIN,
    "rwin": 0x5C,
    "semicolon": 0xBA,
    "plus": 0xBB,
    "equal": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "dot": 0xBE,
    "slash": 0xBF,
    "backtick": 0xC0,
    "grave": 0xC0,
    "lbracket": 0xDB,
    "backslash": 0xDC,
    "rbracket": 0xDD,
    "quote": 0xDE,
}
for _i in range(1, 25):
    _VK["f%d" % _i] = 0x6F + _i

#: char -> (vk, needs_shift)
_PUNCT: dict[str, tuple[int, int]] = {
    ".": (0xBE, 0),
    ",": (0xBC, 0),
    ";": (0xBA, 0),
    "'": (0xDE, 0),
    "[": (0xDB, 0),
    "]": (0xDD, 0),
    "\\": (0xDC, 0),
    "/": (0xBF, 0),
    "-": (0xBD, 0),
    "=": (0xBB, 0),
    "`": (0xC0, 0),
    "!": (0x31, 1),
    "@": (0x32, 1),
    "#": (0x33, 1),
    "$": (0x34, 1),
    "%": (0x35, 1),
    "^": (0x36, 1),
    "&": (0x37, 1),
    "*": (0x38, 1),
    "(": (0x39, 1),
    ")": (0x30, 1),
    "_": (0xBD, 1),
    "+": (0xBB, 1),
    "{": (0xDB, 1),
    "}": (0xDD, 1),
    "|": (0xDC, 1),
    ":": (0xBA, 1),
    '"': (0xDE, 1),
    "<": (0xBC, 1),
    ">": (0xBE, 1),
    "?": (0xBF, 1),
    "~": (0xC0, 1),
}

_MODIFIER_NAMES = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "win": "win", "windows": "win", "cmd": "win", "meta": "win"}
_AHK_MOD = {"^": "ctrl", "+": "shift", "%": "alt", "~": "win"}
_MOD_VK = {"ctrl": VK_CONTROL, "shift": VK_SHIFT, "alt": VK_MENU, "win": VK_LWIN}

_user32 = ctypes.windll.user32


def _send_inputs(inputs: list[_INPUT]) -> None:
    if not inputs:
        return
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))


def _kb(vk: int, up: bool = False, unicode: bool = False, scan: int = 0) -> _INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if unicode:
        flags |= KEYEVENTF_UNICODE
        return _INPUT(type=_INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(0, scan, flags, 0, 0)))
    return _INPUT(type=_INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, 0)))


def _mi(flags: int, data: int = 0, dx: int = 0, dy: int = 0) -> _INPUT:
    return _INPUT(
        type=_INPUT_MOUSE,
        u=_INPUTUNION(mi=_MOUSEINPUT(int(dx), int(dy), data & 0xFFFFFFFF, flags, 0, 0)),
    )


#: Extra spellings people (and SendKeys docs) use.  Compared after
#: ``_norm_key`` (lowercase, spaces/underscores/hyphens removed).
_VK_ALIASES: dict[str, int] = {
    "pageup": 0x21,
    "pgup": 0x21,
    "prior": 0x21,
    "pagedown": 0x22,
    "pgdown": 0x22,
    "pgdn": 0x22,
    "next": 0x22,
    "escape": 0x1B,
    "esc": 0x1B,
    "return": 0x0D,
    "enter": 0x0D,
    "backspace": 0x08,
    "bksp": 0x08,
    "back": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "windows": 0x5B,
    "win": 0x5B,
    "lwin": 0x5B,
    "space": 0x20,
    "spacebar": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "prtscr": 0x2C,
    "tab": 0x09,
    "apps": 0x5D,
    "menu": 0x5D,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "numpadadd": 0x6B,
    "numpadsub": 0x6D,
    "numpadmul": 0x6A,
    "numpaddiv": 0x6F,
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
    "semicolon": 0xBA,
    "plus": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "dot": 0xBE,
    "slash": 0xBF,
    "backtick": 0xC0,
    "grave": 0xC0,
    "lbracket": 0xDB,
    "backslash": 0xDC,
    "rbracket": 0xDD,
    "quote": 0xDE,
}


def _norm_key(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch not in " _-")


_VK_NORM: dict[str, int] = {}
for _n, _v in list(_VK.items()) + list(_VK_ALIASES.items()):
    _VK_NORM[_norm_key(_n)] = _v


def known_key_names() -> list[str]:
    return sorted({k for k in (list(_VK) + list(_VK_ALIASES)) if len(k) > 1})


def _vk_for(key: str) -> tuple[int, int]:
    """(vk, needs_shift) for a key name or a single character."""
    k = key.strip()
    if not k:
        raise BridgeError("空按键")
    nk = _norm_key(k)
    if nk in _VK_NORM:
        return _VK_NORM[nk], 0
    if len(k) == 1:
        ch = k
        if ch in _PUNCT:
            return _PUNCT[ch]
        if ch.isalpha():
            return ord(ch.upper()), 1 if ch.isupper() else 0
        if ch.isdigit():
            return ord(ch), 0
    names = known_key_names()
    raise BridgeError(
        f"不认识的按键: {k!r}；可用按键（大小写/空格不敏感）: {', '.join(names)}"
    )


def _parse_keys(keys: str) -> list[tuple[set[str], str]]:
    """'^s' / 'ctrl+shift+t' / 'alt+f4' / 'enter' / 'ctrl+a delete' -> chords."""
    s = (keys or "").strip()
    if not s:
        raise BridgeError("keys 为空")
    chords: list[tuple[set[str], str]] = []
    for tok in s.split():
        mods: set[str] = set()
        rest = tok
        # AutoHotkey style prefixes: ^s  +{tab}  %{f4}
        while len(rest) > 1 and rest[0] in _AHK_MOD:
            mods.add(_AHK_MOD[rest[0]])
            rest = rest[1:]
        rest = rest.strip("{}")
        # Win32 style: ctrl+shift+t
        if "+" in rest:
            parts = [p for p in rest.split("+")]
            while len(parts) > 1:
                head = parts[0].strip().lower()
                if head in _MODIFIER_NAMES:
                    mods.add(_MODIFIER_NAMES[head])
                    parts.pop(0)
                else:
                    break
            rest = "+".join(parts)
        rest = rest.strip("{}")
        if not rest:
            raise BridgeError(f"无法解析按键: {keys!r}")
        if not mods and len(rest) == 1 and rest in _PUNCT:
            vk, shift = _PUNCT[rest]
            if shift:
                mods.add("shift")
        chords.append((mods, rest))
    return chords


def send_keys(keys: str, *, interval: float = 0.0) -> str:
    """Send a key chord / sequence. Returns a human description.

    Modifiers are real, separated key events — NOT one batched SendInput:

        Shift down  ...12ms...  main key down ...25ms... main key up ...8ms...
        Shift up

    With everything in a single batch, apps that read modifier state when they
    later process the queued event (raw-input consumers such as Blender) see the
    modifier already released and execute the bare key (``shift+f4`` became
    plain ``F4`` = File menu).  Named keys and letters go through the same path.
    """
    chords = _parse_keys(keys)
    desc: list[str] = []
    for mods, key in chords:
        vk, need_shift = _vk_for(key)
        if need_shift:
            mods = set(mods) | {"shift"}
        order = [m for m in ("ctrl", "shift", "alt", "win") if m in mods]
        for m in order:
            _send_inputs([_kb(_MOD_VK[m])])
            time.sleep(0.012)
        time.sleep(0.012)
        _send_inputs([_kb(vk)])
        time.sleep(0.025)
        _send_inputs([_kb(vk, up=True)])
        time.sleep(0.008)
        for m in reversed(order):
            _send_inputs([_kb(_MOD_VK[m], up=True)])
            time.sleep(0.008)
        desc.append("+".join(order + [key]))
    if interval:
        time.sleep(interval)
    return " ".join(desc)


HOLD_MIN_MS = 20
HOLD_MAX_MS = 10000


def _clamp_hold_ms(ms: Any) -> tuple[int, int, bool]:
    """(requested, used, clipped) — holds are clamped to 20..10000ms."""
    try:
        want = int(ms) if ms is not None else 500
    except (TypeError, ValueError):
        raise BridgeError(f"ms 必须是整数毫秒（20-{HOLD_MAX_MS}），收到 {ms!r}") from None
    used = max(HOLD_MIN_MS, min(HOLD_MAX_MS, want))
    return want, used, used != want


def hold_keys(keys: str, ms: int) -> str:
    """Hold a key chord for *ms*: keydown ... sleep ... keyup (never stuck)."""
    chords = _parse_keys(keys)
    if not chords:
        raise BridgeError("hold 需要 keys")
    if len(chords) > 1:
        raise BridgeError("hold 只支持单个组合键（如 shift+w / w），不支持空格分隔的序列")
    mods, key = chords[0]
    vk, need_shift = _vk_for(key)
    if need_shift:
        mods = set(mods) | {"shift"}
    order = [m for m in ("ctrl", "shift", "alt", "win") if m in mods]
    desc = "+".join(order + [key])
    for m in order:
        _send_inputs([_kb(_MOD_VK[m])])
        time.sleep(0.012)
    _send_inputs([_kb(vk)])
    try:
        time.sleep(max(HOLD_MIN_MS, int(ms)) / 1000.0)
    finally:
        # never leave a key logically pressed, even on interrupt
        _send_inputs([_kb(vk, up=True)])
        for m in reversed(order):
            _send_inputs([_kb(_MOD_VK[m], up=True)])
            time.sleep(0.008)
    return desc


def hold_mouse(button: str, ms: int) -> dict:
    """Hold a mouse button for *ms* with same-position heartbeat moves.

    Games that read "button still down" per frame also require fresh MOUSEMOVE
    events, otherwise they treat the press as a stuck key and drop it.
    """
    btn = (button or "left").strip().lower()
    if btn not in ("left", "right", "middle"):
        raise BridgeError(f"button 只能是 left/right/middle，收到 {button!r}")
    pt = wt.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    x, y = int(pt.x), int(pt.y)
    _mouse(btn, True)
    beats = 0
    try:
        remaining = max(HOLD_MIN_MS, int(ms))
        interval = max(60, min(200, remaining // 12))
        while remaining > 0:
            chunk = min(interval, remaining)
            time.sleep(chunk / 1000.0)
            remaining -= chunk
            _move_cursor_abs(x, y)
            beats += 1
    finally:
        _mouse(btn, False)
    return {"beats": beats, "x": x, "y": y}


def type_unicode(text: str) -> None:
    """Type arbitrary unicode text into the focused control (no clipboard)."""
    if not text:
        return
    events: list[_INPUT] = []
    for unit in _utf16_units(text):
        events.append(_kb(0, unicode=True, scan=unit))
        events.append(_kb(0, up=True, unicode=True, scan=unit))
    _send_inputs(events)


def _utf16_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]


def _move_cursor(x: int, y: int) -> None:
    _user32.SetCursorPos(int(x), int(y))


def _move_cursor_abs(x: int, y: int) -> None:
    """Move the cursor with a real MOUSEMOVE event (absolute, virtual desktop).

    SetCursorPos alone updates the cursor without a SendInput move record;
    some apps' drag loops and raw-input consumers ignore that, so while a
    button is held we emit PROPER mouse moves.
    """
    try:
        vx, vy, vw, vh = _win.virtual_screen()
        ax = int(round((x - vx) * 65535 / max(1, vw - 1)))
        ay = int(round((y - vy) * 65535 / max(1, vh - 1)))
        _send_inputs(
            [_mi(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, ax, ay)]
        )
        _user32.SetCursorPos(int(x), int(y))  # keep the exact pixel
    except Exception:
        _user32.SetCursorPos(int(x), int(y))


def _mouse(button: str, down: bool) -> None:
    table = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    flag = table.get(button, table["left"])[0 if down else 1]
    _send_inputs([_mi(flag)])


def _wheel(x: int, y: int, delta: int, horizontal: bool = False) -> None:
    _move_cursor(x, y)
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send_inputs([_mi(flag, int(delta))])


# --------------------------------------------------------------------------- #
# clipboard (uiautomation already provides a working CF_UNICODETEXT bridge)
# --------------------------------------------------------------------------- #
def _clipboard_get() -> str | None:
    return _win.get_clipboard_text()


def _clipboard_set(text: str) -> bool:
    """Write text to the clipboard.  False means "not written" (never silent)."""
    return bool(_win.set_clipboard_text(text))


def _clipboard_write_verified(text: str, attempts: int = 3) -> bool:
    """Write + read back; only True when the clipboard really holds *text*."""
    for i in range(max(1, attempts)):
        if _win.set_clipboard_text(text):
            got = _win.get_clipboard_text()
            if got == (text or ""):
                return True
        time.sleep(0.04 * (i + 1))
    return False


@contextlib.contextmanager
def _clipboard_guard():
    """Restore the previous clipboard text afterwards (politeness)."""
    prev = _clipboard_get()
    try:
        yield
    finally:
        if prev is not None:
            try:
                _clipboard_set(prev)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# element handle
# --------------------------------------------------------------------------- #
@dataclass
class Element:
    """A resolved UIA element plus the node summary it was built from."""

    eid: int
    hwnd: int
    rid: tuple | None
    control: Any
    node: dict = field(default_factory=dict)
    ts: float = 0.0
    #: cache fast path: raw IUIAutomationElement with cached properties; the
    #: live uiautomation Control is materialised lazily from it on first action.
    cached_element: Any = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Element #{self.eid} {self.node.get('ct')!r} {self.node.get('name')!r}>"


def _prop(obj: Any, name: str, default: Any = None) -> Any:
    try:
        v = getattr(obj, name)
        return default if v is None else v
    except Exception:
        return default


def _rect_tuple(rect: Any) -> list[int]:
    try:
        return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:
        return [0, 0, 0, 0]


def _area(r: list[int]) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


_MARK_COLORS = [(255, 64, 64), (64, 160, 255), (46, 204, 113), (255, 176, 32)]


def drag_path(x: int, y: int, tx: int, ty: int, steps: int = 10) -> list[tuple[int, int]]:
    """Interpolated drag path (pure). Must advance on BOTH axes, no int-truncation.

    Unit-tested: regression guard for "the X component silently vanished".
    """
    steps = max(1, int(steps))
    pts: list[tuple[int, int]] = []
    for i in range(1, steps + 1):
        pts.append(
            (
                int(round(x + (tx - x) * i / steps)),
                int(round(y + (ty - y) * i / steps)),
            )
        )
    return pts


def _clamp_int(v: int, lo: int, hi: int) -> int:
    if hi < lo:
        hi = lo
    return max(int(lo), min(int(v), int(hi)))


def collect_marks(nodes: list[dict], crop: tuple[int, int, int, int], *, limit: int = 120) -> list[dict]:
    """Interactive nodes inside *crop* as Set-of-Mark entries (physical pixels).

    Pure function (no COM / no PIL) so it can be unit-tested without a desktop.
    """
    l, t, r, b = crop
    marks: list[dict] = []
    for node in nodes or []:
        if (node.get("ct") or "") not in MARK_CTS:
            continue
        try:
            rect = [int(v) for v in (node.get("rect") or [0, 0, 0, 0])[:4]]
        except Exception:
            continue
        if rect[2] - rect[0] <= 2 or rect[3] - rect[1] <= 2:
            continue
        il, it = max(rect[0], l), max(rect[1], t)
        ir, ib = min(rect[2], r), min(rect[3], b)
        if ir - il <= 2 or ib - it <= 2:
            continue
        marks.append({"id": node.get("id"), "rect": [il, it, ir, ib],
                      "ct": node.get("ct"), "name": (node.get("name") or "")[:60]})
        if limit and len(marks) >= int(limit):
            break
    return marks


def annotate_marks(img, marks: list[dict], origin: list[int], scale: float):
    """Draw numbered boxes for *marks* on a PIL image (pure, returns a new image)."""
    from PIL import Image, ImageDraw  # lazy: importing bridge must not load Pillow

    base = img.convert("RGB")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    scale = scale or 1.0
    for i, mark in enumerate(marks):
        l = int(round((mark["rect"][0] - origin[0]) / scale))
        t = int(round((mark["rect"][1] - origin[1]) / scale))
        r = int(round((mark["rect"][2] - origin[0]) / scale))
        b = int(round((mark["rect"][3] - origin[1]) / scale))
        color = _MARK_COLORS[i % len(_MARK_COLORS)]
        draw.rectangle([l, t, r, b], outline=color + (255,), width=2)
        label = str(mark.get("id"))
        tw = 7 * len(label) + 8
        th = 14
        ly = t - th if t - th >= 0 else t
        draw.rectangle([l, ly, l + tw, ly + th], fill=color + (210,))
        draw.text((l + 4, ly + 1), label, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def _norm(s: str) -> str:
    return "".join((s or "").lower().split())


# --------------------------------------------------------------------------- #
# UIA cache-request fast path
# --------------------------------------------------------------------------- #
# A snapshot is dominated by cross-process reads: the legacy walk pays ~9
# property round-trips per visited node plus RawViewWalker child/sibling calls,
# while IUIAutomationElement.BuildUpdatedCache fetches the whole subtree in
# ONE call and every later read hits a local cache.  Measured on a 580-node
# Chromium window: legacy ~830ms, cached ~70ms (see bench/opt/bench.py).
#
# The cached traversal below mirrors _walk exactly (same DFS order, same
# LEAF_TYPES pruning, same interactive_cap bounded DFS, same RuntimeId ->
# stable-id mapping), so snapshot output is identical.  Rules:
#
# * TreeFilter = RawViewCondition because the legacy walker is uiautomation's
#   RawViewWalker (_AutomationClient.ViewWalker); the default Control-VIEW
#   filter would silently drop raw-only nodes.
# * any exception / suspicious result -> stderr log + transparent fallback to
#   the legacy walk; SUPER_CU_NO_CACHE=1 disables the path entirely.
# * cached elements stay lazy: Element.control is None and the live
#   uiautomation Control is materialised on first action (_materialize), so
#   building a snapshot never pays ControlFromElement's live ControlType call.

#: Environment kill switch: any truthy value forces the legacy per-node walk.
_CACHE_OFF_ENV = "SUPER_CU_NO_CACHE"

#: Properties cached for every element of the window subtree (order = call order).
_CACHE_PROP_NAMES = (
    "ControlTypeProperty",
    "NameProperty",
    "AutomationIdProperty",
    "ClassNameProperty",
    "IsEnabledProperty",
    "IsOffscreenProperty",
    "HasKeyboardFocusProperty",
    "BoundingRectangleProperty",
    "RuntimeIdProperty",
)


def _cache_prop_ids() -> tuple[int, ...]:
    ids: list[int] = []
    for name in _CACHE_PROP_NAMES:
        pid = getattr(auto.PropertyId, name, None)
        if pid is None:  # pragma: no cover - uiautomation ABI guard
            return ()
        ids.append(int(pid))
    return tuple(ids)


_CACHE_PROP_IDS = _cache_prop_ids()

#: BuildUpdatedCache budget: past this a window's fast path is suspended for
#: CACHE_SKIP_S seconds (the current snapshot still uses the already-paid cache).
#: Measured worst case on this machine: 18ms (195-node Chromium), 137ms
#: (128-node Explorer), 72ms (Notepad + 1500-line document); the budget is a
#: safety valve for tree shapes we could not measure, not a normal limit.
_CACHE_BUILD_BUDGET_MS = 250.0
#: How long a window falls back to the legacy walk after a cache failure/slow build.
_CACHE_SKIP_S = 60.0

#: (IUIAutomation client, generated UIAutomationCore comtypes module)
_UIA_CTX: tuple[Any, Any] | None = None


def _uia_ctx() -> tuple[Any, Any] | None:
    """The IUIAutomation client + typelib module used for cache requests.

    Reuses the exact COM client uiautomation already created (one UIA instance
    per process, same apartment); lazily creates our own only as a fallback.
    Never raises.
    """
    global _UIA_CTX
    if _UIA_CTX is not None:
        return _UIA_CTX
    try:
        from uiautomation import uiautomation as _au  # same module, private client

        cli = _au._AutomationClient.instance()
        _UIA_CTX = (cli.IUIAutomation, cli.UIAutomationCore)
        return _UIA_CTX
    except Exception:
        pass
    try:  # pragma: no cover - only when uiautomation internals change
        import comtypes.client

        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        client = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=mod.IUIAutomation
        )
        _UIA_CTX = (client, mod)
        return _UIA_CTX
    except Exception:
        return None


def _cache_enabled() -> bool:
    raw = (os.environ.get(_CACHE_OFF_ENV) or "").strip().lower()
    return raw in ("", "0", "false", "no", "off")


def _cached_attr(el: Any, name: str, default: Any = None) -> Any:
    """Read one Cached* property; default on unavailable (never raises)."""
    try:
        v = getattr(el, name)
        return default if v is None else v
    except Exception:
        return default


def _cached_ct(el: Any) -> str:
    """Cached control type -> long name used by the *_TYPES tables."""
    ct = _cached_attr(el, "CachedControlType", None)
    if ct is None:
        return ""
    try:
        return auto.ControlTypeNames.get(int(ct), "")
    except Exception:
        return ""


def _cached_rect(el: Any) -> list[int]:
    """Cached BoundingRectangle -> [l, t, r, b] (RECT struct or 4-tuple)."""
    rect = _cached_attr(el, "CachedBoundingRectangle", None)
    if rect is None:
        return [0, 0, 0, 0]
    try:  # comtypes RECT struct variant
        return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:
        pass
    try:  # plain (left, top, right, bottom) variant
        vals = list(rect)[:4]
        if len(vals) == 4:
            return [int(v) for v in vals]
    except Exception:
        pass
    return [0, 0, 0, 0]


def _cached_rid(el: Any) -> tuple | None:
    """Cached RuntimeId -> tuple (RuntimeId has no Cached* attribute)."""
    try:
        rid = el.GetCachedPropertyValue(auto.PropertyId.RuntimeIdProperty)
    except Exception:
        return None
    if not rid:
        return None
    try:
        # exactly what the legacy path keys ids on (_runtime_id -> tuple(rid))
        return tuple(rid)
    except Exception:
        return None


def _cached_children(el: Any) -> list[Any]:
    """Cached children as a plain list (IUIAutomationElementArray isn't iterable)."""
    try:
        arr = el.GetCachedChildren()
    except Exception:
        return []
    if not arr:
        return []
    try:
        n = int(arr.Length)
    except Exception:
        return []
    out: list[Any] = []
    for i in range(n):
        try:
            child = arr.GetElement(i)
        except Exception:
            break
        if child:
            out.append(child)
    return out


def _cached_pattern(el: Any, pattern_id: int, iface: Any) -> Any:
    """Typed cached pattern (QueryInterface on the raw IUnknown), or None."""
    try:
        raw = el.GetCachedPattern(pattern_id)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return raw.QueryInterface(iface)
    except Exception:
        return None


def _live_control_from_element(el: Any) -> Any:
    """uiautomation Control for a cached element (1 live ControlType call)."""
    try:
        return auto.Control.CreateControlFromElement(el)
    except Exception:
        return None


class _CacheFallback(Exception):
    """Cached path returned a suspicious result; use the legacy walk instead."""


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #
class Bridge:
    """Stateful UIA bridge: caches ids and the last snapshot per window."""

    def __init__(self, *, lock_timeout_s: float = 30.0, use_cache: bool | None = None) -> None:
        # per-thread, MUST precede any UIA/COM object creation (see _ensure_com)
        _ensure_com()
        _init_uia_globals()
        self._lock_timeout_s = float(lock_timeout_s)
        #: UIA cache-request fast path (see _walk_cached); None -> env switch.
        self._use_cache = _cache_enabled() if use_cache is None else bool(use_cache)
        #: windows whose cached path failed recently: legacy walk for a while.
        self._cache_skip_until: dict[int, float] = {}
        self._cache_warned: set[int] = set()
        self._cache_warm: set[int] = set()
        self._max_ids = int(MAX_IDS)
        self._max_windows = int(MAX_WINDOWS)
        self._rid2id: dict[tuple, int] = {}
        self._id2el: dict[int, Element] = {}
        self._next_id = 1
        self._win_ctl: dict[int, Any] = {}
        self._win_info: dict[int, dict] = {}
        self._cache: dict[int, dict] = {}
        self._last_shot: dict | None = None
        self._last_launch: dict | None = None
        self._last_verified: bool | None = None
        self._space_ok: bool | None = None

    # -- infrastructure ---------------------------------------------------- #
    def space_check(self) -> dict:
        """Prove UIA/win32 space == ImageGrab space, else raise.

        If the process is DPI-unaware on a scaled display, GetSystemMetrics
        reports the virtualised size while the display DC reports physical
        pixels -- exactly the 1.5x error we must never ship silently.
        """
        if self._space_ok is not None:
            return {"ok": self._space_ok, "checked": True}
        sm = _win.screen_size()
        phys = [0, 0]
        try:
            hdc = _user32.GetDC(None)
            if hdc:
                try:
                    phys = [
                        int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 8)),  # HORZRES
                        int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 10)),  # VERTRES
                    ]
                finally:
                    _user32.ReleaseDC(None, hdc)
        except Exception:
            phys = list(sm)
        ok = abs(phys[0] - sm[0]) <= 1 and abs(phys[1] - sm[1]) <= 1
        state = {
            "ok": ok,
            "screen": list(sm),
            "physical": phys,
            "dpi": DPI_STATE,
        }
        if not ok:
            raise BridgeError(
                "坐标空间不一致：进程 DPI awareness 未生效 "
                f"(GetSystemMetrics={sm}, 显示物理={phys})。"
                "拒绝继续，避免点击偏移 1.5 倍；请先调用 dpi.ensure_dpi_aware()。"
            )
        self._space_ok = True
        return state

    def _control_for(self, hwnd: int) -> Any:
        hwnd = int(hwnd)
        ctl = self._win_ctl.get(hwnd)
        if ctl is not None:
            return ctl
        if not _user32.IsWindow(wt.HWND(hwnd)):
            raise BridgeError(f"窗口 {hwnd} 不存在（可能已关闭）")
        ctl = auto.ControlFromHandle(hwnd)
        if not ctl:
            raise BridgeError(f"窗口 {hwnd} 无法通过 UIA 访问（可能已失效或权限不足）")
        self._win_ctl.pop(hwnd, None)  # re-insert at the end = most recent
        self._win_ctl[hwnd] = ctl
        self._trim_windows()
        return ctl

    def _drop_window(self, hwnd: int) -> None:
        self._win_ctl.pop(int(hwnd), None)
        self._cache.pop(int(hwnd), None)

    def _remember_window(self, info: dict) -> None:
        """Cache title/proc/pid so a re-created UWP window can be found again."""
        hwnd = int(info.get("hwnd") or 0)
        if hwnd:
            self._win_info.pop(hwnd, None)  # re-insert at the end = most recent
            self._win_info[hwnd] = {
                "title": info.get("title") or "",
                "proc": info.get("proc") or "",
                "pid": info.get("pid") or 0,
                "class": info.get("class") or "",
            }
            self._trim_windows()

    def _rehome(self, gone_hwnd: int) -> int | None:
        """Find the replacement of a window that was closed/re-created.

        UWP apps (ApplicationFrameHost) may replace the top-level hwnd during
        activation; the app is still the same one.  We look for it by pid, then
        exact title, then process name — and never invent a window.
        """
        info = self._win_info.get(int(gone_hwnd)) or {}
        try:
            wins = _win.list_windows()
        except Exception:
            return None
        pid = int(info.get("pid") or 0)
        if pid:
            same = [w for w in wins if w["pid"] == pid]
            if same:
                best = max(same, key=lambda w: _area(w["rect"]))
                self._remember_window(best)
                return int(best["hwnd"])
        title = (info.get("title") or "").strip()
        if title:
            hits = [w for w in wins if (w["title"] or "").strip() == title]
            if len(hits) == 1:
                self._remember_window(hits[0])
                return int(hits[0]["hwnd"])
        proc = (info.get("proc") or "").strip().lower()
        if proc:
            hits = [w for w in wins if w["proc"].lower() == proc]
            if len(hits) == 1:
                self._remember_window(hits[0])
                return int(hits[0]["hwnd"])
        return None

    @contextlib.contextmanager
    def _locked(self):
        """Re-entrant wrapper around gui_lock.gui_locked().

        ``act()`` takes the desktop lock itself for every action that changes
        focus / keyboard / mouse, so callers must NOT wrap ``act()`` in
        ``gui_locked()`` (that would just look like a nested hold here, which
        this guard absorbs).  A module level counter keeps nesting from
        dead-locking on the file lock, across Bridge instances.
        """
        global _GUI_DEPTH
        nested = False
        with _GUI_DEPTH_LOCK:
            if _GUI_DEPTH:
                _GUI_DEPTH += 1
                nested = True
        if nested:
            try:
                yield
            finally:
                with _GUI_DEPTH_LOCK:
                    _GUI_DEPTH -= 1
            return
        with gui_locked(timeout_s=self._lock_timeout_s):
            with _GUI_DEPTH_LOCK:
                _GUI_DEPTH = 1
            try:
                yield
            finally:
                with _GUI_DEPTH_LOCK:
                    _GUI_DEPTH = 0

    @staticmethod
    def _lock_held_by_this_process() -> bool:
        """True while this module holds the desktop lock (debug/introspection)."""
        return _GUI_DEPTH > 0

    # -- ids --------------------------------------------------------------- #
    def _id_for(self, key: tuple | None) -> int:
        if key is None:
            return 0
        eid = self._rid2id.pop(key, None)
        if eid is None:
            eid = self._next_id
            self._next_id += 1
            # bounded retention: drop the least recently used element ids
            while len(self._rid2id) >= self._max_ids:
                old_key, old_eid = next(iter(self._rid2id.items()))
                del self._rid2id[old_key]
                self._id2el.pop(old_eid, None)
        self._rid2id[key] = eid  # (re)insert at the end = most recently used
        return eid

    def _remember_controls(self, controls: dict[int, Element]) -> None:
        """Store elements in the id map, bounded and most-recently-used last.

        Ids keep resolving after this (by_id never raises); only ids older than
        the last MAX_IDS distinct elements are recycled.
        """
        for eid in controls:
            self._id2el.pop(eid, None)  # re-insert at the end = most recent
        self._id2el.update(controls)
        while len(self._id2el) > self._max_ids:
            self._id2el.pop(next(iter(self._id2el)), None)

    def _trim_windows(self) -> None:
        """Bound the window-keyed maps (_win_ctl / _win_info / _cache)."""
        for d in (self._win_ctl, self._win_info, self._cache):
            while len(d) > self._max_windows:
                d.pop(next(iter(d)), None)

    def _runtime_id(self, ctl: Any) -> tuple | None:
        try:
            rid = ctl.GetRuntimeId()
        except Exception:
            return None
        if not rid:
            return None
        return tuple(rid)

    def by_id(self, eid: int) -> Element | None:
        """Look up a previously returned id.  Never touches COM, never raises.

        The id map is bounded (MAX_IDS): after a very long session the oldest
        ids are dropped, so a miss only means "run cu_show again" -- actionable.
        """
        try:
            return self._id2el.get(int(eid))
        except Exception:
            return None

    # -- walk -------------------------------------------------------------- #
    def _keep(self, ct: str, name: str, nkids: int, view: str, name_filter: str | None) -> bool:
        if name_filter:
            nf = _norm(name_filter)
            return bool(nf) and nf in _norm(name)
        if view == "all":
            return True
        if view == "text":
            if ct in TEXT_TYPES:
                return True
            if ct in INTERACTIVE_TYPES:
                return bool(name.strip())
            if ct in CONTAINER_TYPES:
                return bool(name.strip()) and nkids == 0
            return False
        # controls (default)
        if ct in INTERACTIVE_TYPES:
            return True
        if ct in TEXT_TYPES:
            return bool(name.strip())
        if ct in CONTAINER_TYPES:
            return bool(name.strip()) and nkids == 0
        return False

    def _walk(
        self,
        root: Any,
        *,
        max_depth: int = 24,
        view: str = "controls",
        name_filter: str | None = None,
        interactive_cap: int = 0,
    ) -> tuple[list[tuple], int, bool]:
        """DFS the window's subtree; returns (candidates, visited, cut).

        Two performance rules that do NOT change the output:

        * bounded DFS: with the controls view the final ranking keeps the first
          ``max_nodes`` interactive nodes in document order, so once
          ``interactive_cap`` of them are collected nothing deeper can ever be
          shown -> stop walking (``cut=True``).
        * lazy properties: ``Name`` is only read where it decides candidacy or
          the breadcrumb (text / container types).  Interactive types are
          candidates by type alone; their name is read later for the nodes that
          are actually displayed.
        """
        cands: list[tuple] = []
        visited = 0
        depth_cut = False
        interactive = 0
        stack: list[tuple] = [(root, 0, ())]
        lazy_names = False  # measured: deferring Name costs more than it saves here
        while stack:
            ctl, depth, path = stack.pop()
            visited += 1
            ct = _prop(ctl, "ControlTypeName", "") or ""
            if ct in LEAF_TYPES:
                kids: list = []
            else:
                try:
                    kids = ctl.GetChildren() or []
                except Exception:
                    kids = []
            nkids = len(kids)
            need_name = (
                not lazy_names
                or ct in TEXT_TYPES
                or ct in CONTAINER_TYPES
                or ct in LEAF_TYPES
                and ct not in INTERACTIVE_TYPES
            )
            name = (_prop(ctl, "Name", "") or "") if need_name else ""
            if self._keep(ct, name, nkids, view, name_filter):
                cands.append((ctl, depth, path, ct, name, nkids))
                if ct in INTERACTIVE_TYPES:
                    interactive += 1
                    if interactive_cap and interactive >= interactive_cap:
                        depth_cut = True  # bounded DFS: nothing deeper can be shown
                        break
            if kids:
                if depth + 1 <= max_depth:
                    npath = path + (name.strip(),) if name.strip() else path
                    npath = npath[-3:]
                    for k in reversed(kids):
                        stack.append((k, depth + 1, npath))
                else:
                    depth_cut = True
        return cands, visited, depth_cut

    def _rank(self, cand: tuple) -> int:
        ct = cand[3]
        if ct in INTERACTIVE_TYPES:
            return 0
        if ct in TEXT_TYPES:
            return 1
        return 2

    def _make_node(
        self, cand: tuple, *, with_value: bool = True, with_doc_text: bool = False
    ) -> tuple[dict | None, Element | None]:
        ctl, depth, path, ct, name, nkids = cand
        if not name and ct in INTERACTIVE_TYPES:
            # lazy-name walk: interactive types are candidates by type alone
            name = _prop(ctl, "Name", "") or ""
        short = CT_SHORT.get(ct, "other")
        rid = self._runtime_id(ctl)
        rect = _rect_tuple(_prop(ctl, "BoundingRectangle", None))
        aid = _prop(ctl, "AutomationId", "") or ""
        cls = _prop(ctl, "ClassName", "") or ""
        enabled = _prop(ctl, "IsEnabled", True)
        offscreen = _prop(ctl, "IsOffscreen", False)
        focused = _prop(ctl, "HasKeyboardFocus", False)
        key = rid if rid is not None else ("noid", ct, name, aid, tuple(rect))
        eid = self._id_for(key)

        val = None
        if with_value and short in ("edit", "combobox"):
            try:
                vp = ctl.GetValuePattern()
                if vp:
                    val = vp.Value
            except Exception:
                val = None
        if with_doc_text and short == "document":
            try:
                tp = ctl.GetTextPattern()
                if tp:
                    val = (tp.DocumentRange.GetText(2000) or "")[:2000]
            except Exception:
                val = None

        if _area(rect) == 0 and bool(offscreen) and not name:
            return None, None

        node = {
            "id": eid,
            "ct": short,
            "name": name,
            "val": val,
            "aid": aid or None,
            "cls": cls or None,
            "rect": rect,
            "enabled": bool(enabled) if enabled is not None else True,
            "offscreen": bool(offscreen),
            "focused": bool(focused),
            "patterns": list(PATTERN_HINTS.get(short, [])),
            "kids": nkids,
            "depth": depth,
            "path": "/".join(path),
        }
        el = Element(eid=eid, hwnd=0, rid=rid, control=ctl, node=node, ts=time.monotonic())
        return node, el

    # -- cache fast path --------------------------------------------------- #
    def _cache_note(self, hwnd: int, why: str) -> None:
        """Log one fallback reason per window to stderr (never spam per call)."""
        hwnd = int(hwnd or 0)
        if hwnd in self._cache_warned:
            return
        self._cache_warned.add(hwnd)
        try:
            print(
                f"[supercu] UIA cache fast-path 不可用，回退 legacy walk (hwnd={hwnd}): {why}",
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            pass

    def _cache_fail(self, hwnd: int, why: str) -> None:
        """Log once and stop retrying the cache path for this window for a while."""
        self._cache_note(hwnd, why)
        self._cache_skip_until[int(hwnd or 0)] = time.monotonic() + _CACHE_SKIP_S

    def _cache_usable(self, hwnd: int) -> bool:
        if not self._use_cache:
            return False
        until = self._cache_skip_until.get(int(hwnd or 0))
        return not (until and time.monotonic() < until)

    def _walk_cached(
        self,
        root_el: Any,
        *,
        max_depth: int = 24,
        view: str = "controls",
        name_filter: str | None = None,
        interactive_cap: int = 0,
    ) -> tuple[list[tuple], int, bool, int]:
        """DFS the cached subtree; mirrors _walk exactly (order/pruning/etc.).

        Returns (candidates, visited, cut, root_children).  Candidates use the
        same tuple shape as _walk(), but hold a raw cached IUIAutomationElement
        instead of a uiautomation Control.
        """
        cands: list[tuple] = []
        visited = 0
        depth_cut = False
        interactive = 0
        root_kids = 0
        stack: list[tuple] = [(root_el, 0, ())]
        while stack:
            el, depth, path = stack.pop()
            visited += 1
            ct = _cached_ct(el)
            if ct in LEAF_TYPES:
                kids: list = []
            else:
                kids = _cached_children(el)
            nkids = len(kids)
            if visited == 1:
                root_kids = nkids
            name = _cached_attr(el, "CachedName", "") or ""
            if self._keep(ct, name, nkids, view, name_filter):
                cands.append((el, depth, path, ct, name, nkids))
                if ct in INTERACTIVE_TYPES:
                    interactive += 1
                    if interactive_cap and interactive >= interactive_cap:
                        depth_cut = True  # bounded DFS: nothing deeper can be shown
                        break
            if kids:
                if depth + 1 <= max_depth:
                    npath = path + (name.strip(),) if name.strip() else path
                    npath = npath[-3:]
                    for k in reversed(kids):
                        stack.append((k, depth + 1, npath))
                else:
                    depth_cut = True
        return cands, visited, depth_cut, root_kids

    def _cached_cands(
        self,
        root: Any,
        *,
        hwnd: int,
        max_depth: int,
        view: str,
        name_filter: str | None,
        interactive_cap: int,
    ) -> tuple[list[tuple], int, bool, Any]:
        """One BuildUpdatedCache + cached DFS.

        Returns (candidates, visited, cut, comtypes module) or raises
        _CacheFallback/anything else -- both mean "use the legacy walk".
        """
        if not _CACHE_PROP_IDS:  # pragma: no cover - uiautomation ABI guard
            raise _CacheFallback("缺少 UIA 属性 id")
        ctx = _uia_ctx()
        if ctx is None:
            raise _CacheFallback("无法获取 IUIAutomation COM 客户端")
        client, mod = ctx
        req = client.CreateCacheRequest()
        for pid in _CACHE_PROP_IDS:
            req.AddProperty(pid)
        # ValuePattern is needed for edit/combobox values in every view;
        # TextPattern only for view=text documents -- keep the cache request cheap.
        req.AddPattern(auto.PatternId.ValuePattern)
        if view == "text":
            req.AddPattern(auto.PatternId.TextPattern)
        req.TreeScope = mod.TreeScope_Subtree
        req.TreeFilter = client.RawViewCondition  # legacy walker IS the raw view walker
        root_el = root.Element
        if not root_el:
            raise _CacheFallback("窗口根元素不可用")
        t_build = time.perf_counter()
        cached_root = root_el.BuildUpdatedCache(req)
        build_ms = (time.perf_counter() - t_build) * 1000.0
        if not cached_root:
            raise _CacheFallback("BuildUpdatedCache 返回空")
        # adaptive guard: the build is the expensive half of the fast path, so a
        # pathological window must not pay it on every snapshot.  The result we
        # already paid for is used once, then the window skips the cache for
        # CACHE_SKIP_S seconds (first build per window is warm-up, not counted).
        warm = int(hwnd) in self._cache_warm
        self._cache_warm.add(int(hwnd))
        if warm and build_ms > _CACHE_BUILD_BUDGET_MS:
            self._cache_fail(
                hwnd,
                f"BuildUpdatedCache {build_ms:.0f}ms 超过预算 "
                f"{_CACHE_BUILD_BUDGET_MS:.0f}ms（本次仍使用缓存结果，{_CACHE_SKIP_S:.0f}s 内改走 legacy）",
            )
        # Sanity: the cached tree must be the same element the legacy walk starts
        # from, and a non-leaf root with zero cached children means a broken tree.
        live_ct = _prop(root, "ControlTypeName", "") or ""
        cached_ct = _cached_ct(cached_root)
        if not live_ct:
            raise _CacheFallback("根元素 live 类型不可用")
        if cached_ct and cached_ct != live_ct:
            raise _CacheFallback(f"根元素类型不一致 cached={cached_ct!r} live={live_ct!r}")
        cands, visited, depth_cut, root_kids = self._walk_cached(
            cached_root,
            max_depth=max_depth,
            view=view,
            name_filter=name_filter,
            interactive_cap=interactive_cap,
        )
        if visited <= 0:
            raise _CacheFallback("cached 遍历 visited=0")
        if root_kids == 0 and live_ct not in LEAF_TYPES:
            raise _CacheFallback(f"cached 根节点无子节点（{live_ct}）")
        return cands, visited, depth_cut, mod

    def _cached_value(self, el: Any, mod: Any) -> Any:
        """ValuePattern.Value from the cache (live read as fallback)."""
        pat = _cached_pattern(el, auto.PatternId.ValuePattern, mod.IUIAutomationValuePattern)
        if pat is not None:
            try:
                return pat.CurrentValue
            except Exception:
                pass
        ctl = _live_control_from_element(el)
        try:
            vp = ctl.GetValuePattern() if ctl else None
            return vp.Value if vp else None
        except Exception:
            return None

    def _cached_doc_text(self, el: Any, mod: Any) -> Any:
        """TextPattern.DocumentRange text from the cache (live read as fallback)."""
        pat = _cached_pattern(el, auto.PatternId.TextPattern, mod.IUIAutomationTextPattern)
        if pat is not None:
            try:
                return (pat.DocumentRange.GetText(2000) or "")[:2000]
            except Exception:
                pass
        ctl = _live_control_from_element(el)
        try:
            tp = ctl.GetTextPattern() if ctl else None
            return (tp.DocumentRange.GetText(2000) or "")[:2000] if tp else None
        except Exception:
            return None

    def _make_node_cached(
        self, cand: tuple, *, mod: Any, with_value: bool = True, with_doc_text: bool = False
    ) -> tuple[dict | None, Element | None]:
        """_make_node() built from cached properties (no live property reads)."""
        el, depth, path, ct, name, nkids = cand
        short = CT_SHORT.get(ct, "other")
        rid = _cached_rid(el)
        rect = _cached_rect(el)
        aid = _cached_attr(el, "CachedAutomationId", "") or ""
        cls = _cached_attr(el, "CachedClassName", "") or ""
        enabled = _cached_attr(el, "CachedIsEnabled", True)
        offscreen = _cached_attr(el, "CachedIsOffscreen", False)
        focused = _cached_attr(el, "CachedHasKeyboardFocus", False)
        key = rid if rid is not None else ("noid", ct, name, aid, tuple(rect))
        eid = self._id_for(key)

        val = None
        if with_value and short in ("edit", "combobox"):
            val = self._cached_value(el, mod)
        if with_doc_text and short == "document":
            val = self._cached_doc_text(el, mod)

        if _area(rect) == 0 and bool(offscreen) and not name:
            return None, None

        node = {
            "id": eid,
            "ct": short,
            "name": name,
            "val": val,
            "aid": aid or None,
            "cls": cls or None,
            "rect": rect,
            "enabled": bool(enabled) if enabled is not None else True,
            "offscreen": bool(offscreen),
            "focused": bool(focused),
            "patterns": list(PATTERN_HINTS.get(short, [])),
            "kids": nkids,
            "depth": depth,
            "path": "/".join(path),
        }
        node_el = Element(
            eid=eid,
            hwnd=0,
            rid=rid,
            control=None,
            node=node,
            ts=time.monotonic(),
            cached_element=el,
        )
        return node, node_el

    # -- snapshot ---------------------------------------------------------- #
    def snapshot(
        self,
        hwnd: int,
        *,
        view: str = "controls",
        max_nodes: int = 220,
        max_depth: int = 24,
        name_filter: str | None = None,
    ) -> dict:
        t0 = time.perf_counter()
        hwnd = int(hwnd)
        self.space_check()
        try:
            info = _win.window_info(hwnd)
        except Exception as exc:  # WindowGone / invalid handle
            raise BridgeError(f"窗口 #{hwnd} 已不存在或已关闭：{exc}") from exc
        self._remember_window(info)
        root = self._control_for(hwnd)
        view = (view or "controls").lower()
        if view not in ("controls", "text", "all"):
            raise BridgeError(f"未知 view: {view}")
        interactive_cap = (
            int(max_nodes or 0) if (view == "controls" and not name_filter) else 0
        )
        cands: list[tuple] | None = None
        visited = 0
        depth_cut = False
        cached_mod: Any = None
        if self._cache_usable(hwnd):
            try:
                cands, visited, depth_cut, cached_mod = self._cached_cands(
                    root,
                    hwnd=hwnd,
                    max_depth=max_depth,
                    view=view,
                    name_filter=name_filter,
                    interactive_cap=interactive_cap,
                )
            except Exception as exc:  # ANY cache/COM hiccup -> legacy walk
                cands = None
                cached_mod = None
                self._cache_fail(hwnd, f"{type(exc).__name__}: {exc}")
        if cands is None:
            try:
                cands, visited, depth_cut = self._walk(
                    root,
                    max_depth=max_depth,
                    view=view,
                    name_filter=name_filter,
                    interactive_cap=interactive_cap,
                )
            except Exception:
                # Window tree was rebuilt underneath us -- retry once with a fresh root.
                self._drop_window(hwnd)
                root = self._control_for(hwnd)
                cands, visited, depth_cut = self._walk(
                    root,
                    max_depth=max_depth,
                    view=view,
                    name_filter=name_filter,
                    interactive_cap=interactive_cap,
                )

        truncated = bool(depth_cut)
        total = visited
        if max_nodes and len(cands) > int(max_nodes):
            order = sorted(range(len(cands)), key=lambda i: (self._rank(cands[i]), i))
            keep = sorted(order[: int(max_nodes)])
            cands = [cands[i] for i in keep]
            truncated = True

        nodes: list[dict] = []
        seen: set[int] = set()
        controls: dict[int, Element] = {}
        doc_text = view == "text"
        for cand in cands:
            if cached_mod is not None:
                node, el = self._make_node_cached(
                    cand, mod=cached_mod, with_doc_text=doc_text
                )
            else:
                node, el = self._make_node(cand, with_doc_text=doc_text)
            if node is None or el is None:
                continue
            if node["id"] in seen:
                continue
            seen.add(node["id"])
            node["depth"] = min(node["depth"], 24)
            el.hwnd = hwnd
            nodes.append(node)
            controls[node["id"]] = el

        self._remember_controls(controls)
        self._cache.pop(hwnd, None)  # re-insert at the end = most recent
        self._cache[hwnd] = {"nodes": nodes, "ts": time.monotonic(), "controls": controls}
        self._trim_windows()
        elapsed = int(round((time.perf_counter() - t0) * 1000))
        return {
            "hwnd": hwnd,
            "title": info["title"],
            "rect": info["rect"],
            "focused": info["focused"],
            "proc": info["proc"],
            "space": "physical",
            "view": view,
            "elapsed_ms": elapsed,
            "total": total,
            "shown": len(nodes),
            "truncated": truncated,
            "nodes": nodes,
        }

    # -- resolution -------------------------------------------------------- #
    def _point_from_spec(self, spec: str) -> tuple[int, int]:
        s = (spec or "").strip()
        if s.startswith("@"):
            s = s[1:]
        if s.startswith("n:"):
            shot = self._last_shot
            if not shot:
                raise BridgeError("@n: 需要先调用一次 screenshot()")
            try:
                nx, ny = [float(v) for v in s[2:].split(",")[:2]]
            except Exception as exc:
                raise BridgeError(f"归一化坐标格式应为 @n:0.5,0.5: {spec!r}") from exc
            ox, oy = shot["origin"]
            return (
                int(round(ox + nx * shot.get("screen_w", shot["img_w"]))),
                int(round(oy + ny * shot.get("screen_h", shot["img_h"]))),
            )
        if s.startswith("img:"):
            shot = self._last_shot
            if not shot:
                raise BridgeError("@img: 需要先调用一次 screenshot()")
            try:
                ix, iy = [float(v) for v in s[4:].split(",")[:2]]
            except Exception as exc:
                raise BridgeError(f"图片坐标格式应为 @img:640,360: {spec!r}") from exc
            ox, oy = shot["origin"]
            return int(round(ox + ix * shot["scale"])), int(round(oy + iy * shot["scale"]))
        try:
            x, y = [float(v) for v in s.split(",")[:2]]
        except Exception as exc:
            raise BridgeError(f"坐标格式应为 @x,y / @n:x,y / @img:x,y: {spec!r}") from exc
        return int(round(x)), int(round(y))

    def _node_at_point(self, x: int, y: int) -> tuple[Any, tuple | None, dict]:
        try:
            ctl = auto.ControlFromPoint(int(x), int(y))
        except Exception as exc:
            raise BridgeError(f"({x},{y}) 处 UIA 查询失败: {exc}") from exc
        if not ctl:
            raise BridgeError(f"({x},{y}) 处没有可访问的 UIA 元素")
        deepest = ctl
        # climb to the first element with a usable name / interactive type
        chosen = ctl
        for _ in range(6):
            ct = _prop(ctl, "ControlTypeName", "") or ""
            name = (_prop(ctl, "Name", "") or "").strip()
            if ct in INTERACTIVE_TYPES or (name and ct not in ("PaneControl", "WindowControl")):
                chosen = ctl
                break
            parent = None
            try:
                parent = ctl.GetParentControl()
            except Exception:
                parent = None
            if not parent:
                break
            ctl = parent
            chosen = ctl
        node, _el = self._make_node((chosen, 0, (), _prop(chosen, "ControlTypeName", "") or "", _prop(chosen, "Name", "") or "", len(_safe_children(chosen))), with_value=True)
        if node is None:
            ct = _prop(chosen, "ControlTypeName", "") or ""
            node = {
                "id": 0,
                "ct": CT_SHORT.get(ct, "other"),
                "name": _prop(chosen, "Name", "") or "",
                "val": None,
                "aid": None,
                "cls": None,
                "rect": _rect_tuple(_prop(chosen, "BoundingRectangle", None)),
                "enabled": True,
                "offscreen": False,
                "focused": False,
                "patterns": [],
                "kids": 0,
                "depth": 0,
                "path": "",
            }
        return chosen, self._runtime_id(chosen), node

    def _element_for_control(
        self, ctl: Any, hwnd: int, depth: int = 0, path: tuple = (), ct: str = "", name: str = ""
    ) -> Element:
        ct = ct or (_prop(ctl, "ControlTypeName", "") or "")
        name = name or (_prop(ctl, "Name", "") or "")
        node, el = self._make_node((ctl, depth, path, ct, name, 0))
        if node is None:
            node = {
                "id": self._id_for(self._runtime_id(ctl)),
                "ct": CT_SHORT.get(ct, "other"),
                "name": name,
                "val": None,
                "aid": None,
                "cls": None,
                "rect": _rect_tuple(_prop(ctl, "BoundingRectangle", None)),
                "enabled": True,
                "offscreen": False,
                "focused": False,
                "patterns": list(PATTERN_HINTS.get(CT_SHORT.get(ct, "other"), [])),
                "kids": 0,
                "depth": depth,
                "path": "/".join(path),
            }
            el = Element(node["id"], hwnd, self._runtime_id(ctl), ctl, node, time.monotonic())
        assert el is not None
        el.hwnd = int(hwnd)
        self._remember_controls({el.eid: el})
        return el

    def _search(self, hwnd: int, ct_wanted: str | None, name_pat: str) -> list[tuple]:
        """Match candidates by short type + name (exact tier, then substring)."""
        if not name_pat:
            raise BridgeError("选择器缺少名字，形如 button:保存 / edit:*搜索*")
        wildcard = "*" in name_pat or "?" in name_pat
        pat = _norm(name_pat)
        root = self._control_for(hwnd)
        cands, _visited, _cut = self._walk(root, max_depth=32, view="all", name_filter=None)
        hits: list[tuple] = []
        for cand in cands:
            ctl, depth, path, ct, name, nkids = cand
            short = CT_SHORT.get(ct, "other")
            if ct_wanted and short != ct_wanted:
                continue
            nname = _norm(name)
            if not nname:
                continue
            if wildcard:
                if fnmatch.fnmatchcase(nname, pat):
                    hits.append(cand)
            elif nname == pat:
                hits.append(cand)
        if hits:
            return hits
        if wildcard:
            return []
        subs = []
        for cand in cands:
            ctl, depth, path, ct, name, nkids = cand
            if ct_wanted and CT_SHORT.get(ct, "other") != ct_wanted:
                continue
            if pat and pat in _norm(name):
                subs.append(cand)
        return subs

    def resolve(self, spec: str, hwnd: int) -> tuple[Element, dict]:
        """Parse #id / ct:name / @x,y / @n:x,y / @img:x,y."""
        hwnd = int(hwnd)
        s = (spec or "").strip()
        if not s:
            raise BridgeError("target 为空")
        if s.startswith("#"):
            try:
                eid = int(s[1:])
            except ValueError as exc:
                raise BridgeError(f"元素 id 格式应为 #123: {spec!r}") from exc
            el = self.by_id(eid)
            if el is None:
                raise BridgeError(
                    f"元素 id {eid} 未知（可能已被回收：长时间运行只保留最近的元素；"
                    f"也可能不在本次快照中），请先 cu_show 重新获取"
                )
            _ctl, how = self._revive(el, hwnd)
            meta = {"via": "id", "node": el.node, "candidates": []}
            if how and how not in ("live", "rid"):
                meta["relocated"] = how
            return el, meta
        if s.startswith("@") or re.match(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$", s):
            x, y = self._point_from_spec(s)
            ctl, rid, node = self._node_at_point(x, y)
            el = Element(node.get("id") or 0, hwnd, rid, ctl, node, time.monotonic())
            return el, {"via": "point", "node": node, "candidates": [], "point": [x, y]}

        ct_wanted = None
        name_pat = s
        m = re.match(r"^([A-Za-z]+):(.*)$", s)
        if m and m.group(1).lower() in SHORT_CT:
            ct_wanted = m.group(1).lower()
            name_pat = m.group(2)
        hits = self._search(hwnd, ct_wanted, name_pat)
        cand = self._pick(hits, spec, name_pat)
        if cand is None:
            raise BridgeError(f"未找到目标 {spec!r}，请先 cu_show 确认元素名/类型")
        el = self._element_for_control(cand[0], hwnd, cand[1], cand[2], cand[3], cand[4])
        return el, {"via": "selector", "node": el.node, "candidates": []}

    def _summarize(self, cand: tuple) -> dict:
        ctl, depth, path, ct, name, nkids = cand
        node = None
        if ctl is not None:
            try:
                node, _ = self._make_node(cand, with_value=False)
            except Exception:
                node = None
        if node is None:
            node = {
                "id": 0,
                "ct": CT_SHORT.get(ct, "other"),
                "name": name,
                "rect": [0, 0, 0, 0],
            }
        return {
            "id": node.get("id", 0),
            "ct": node.get("ct"),
            "name": (node.get("name") or "")[:40],
            "rect": node.get("rect"),
        }

    def _pick(self, hits: list[tuple], spec: str, name_pat: str) -> tuple | None:
        """Precision first: one hit, or exact-name disambiguation, else raise."""
        if not hits:
            return None
        if len(hits) > 1:
            target = _norm((name_pat or "").strip("*"))
            exact = [h for h in hits if _norm(h[4]) == target]
            if len(exact) == 1:
                return exact[0]
        if len(hits) == 1:
            return hits[0]
        cands = [self._summarize(h) for h in hits[:8]]
        desc = ", ".join(f"#{c['id']} {c['ct']} {c['name'][:24]!r}" for c in cands)
        raise AmbiguousError(
            f"{spec!r} 匹配到 {len(hits)} 个元素，拒绝猜测：{desc}", cands
        )

    def _adopt(self, el: Element, cand: tuple, hwnd: int, what: str) -> tuple[Any, str]:
        """Re-point *el* at *cand*, keeping its public id stable."""
        new_el = self._element_for_control(cand[0], hwnd, cand[1], cand[2], cand[3], cand[4])
        el.control = new_el.control
        el.rid = new_el.rid
        el.node = new_el.node
        el.cached_element = new_el.cached_element
        self._remember_controls({el.eid: el})
        if what == "rid":
            return el.control, "rid"
        return el.control, f"relocated={what}"

    def _relocate(self, el: Element, cands: list[tuple], hwnd: int) -> tuple[Any, str] | None:
        """Semantic relocation after a UIA tree rebuild (UWP/Chromium).

        Order, exactly as the precision rules require:
          1. same ct + same name, unique
          2. same ct + same AutomationId, unique
          3. same ct + same ancestor path, unique
        Never guesses when the match is ambiguous.
        """
        node = el.node or {}
        want_ct = node.get("ct") or ""
        want_name = node.get("name") or ""
        want_aid = node.get("aid") or ""
        want_path = node.get("path") or ""
        if not cands:
            return None
        same_ct = [c for c in cands if CT_SHORT.get(c[3], "other") == want_ct]
        if want_name.strip():
            hits = [c for c in same_ct if _norm(c[4]) == _norm(want_name)]
            if len(hits) == 1:
                return self._adopt(el, hits[0], hwnd, f"ct:name:{want_name[:24]}")
        if want_aid.strip():
            hits = []
            for c in same_ct[:300]:
                try:
                    if (_prop(c[0], "AutomationId", "") or "") == want_aid:
                        hits.append(c)
                except Exception:
                    pass
                if len(hits) > 1:
                    break
            if len(hits) == 1:
                return self._adopt(el, hits[0], hwnd, f"aid:{want_aid[:24]}")
        if want_path.strip():
            hits = [c for c in same_ct if "/".join(c[2]) == want_path]
            if len(hits) == 1:
                return self._adopt(el, hits[0], hwnd, f"path:{want_path[-30:]}")
        return None

    def _materialize(self, el: Element) -> Any:
        """Live uiautomation Control for *el* (cached fast-path nodes are lazy).

        Snapshot nodes only hold a cached IUIAutomationElement; creating the real
        Control eagerly would cost one live ControlType call per node.  It is
        created here on the first action instead.  ControlTypeName below is then
        a LIVE read, so a rebuilt tree still falls through to the relocate path.
        """
        ctl = el.control
        if ctl is not None:
            return ctl
        cached = el.cached_element
        if cached is None:
            return None
        ctl = _live_control_from_element(cached)
        el.control = ctl
        return ctl

    def _revive(self, el: Element, hwnd: int) -> tuple[Any, str]:
        """Return (live control, how); relocate by RuntimeId then semantically."""
        ctl = self._materialize(el)
        try:
            if _prop(ctl, "ControlTypeName", None):
                return ctl, "live"
        except Exception:
            pass
        node = el.node or {}
        cands: list[tuple] = []
        try:
            root = self._control_for(hwnd)
            cands, _v, _c = self._walk(root, max_depth=32, view="all")
        except BridgeError:
            cands = []
        rid = el.rid
        if rid is not None:
            for cand in cands:
                if self._runtime_id(cand[0]) == rid:
                    return self._adopt(el, cand, hwnd, "rid")
        found = self._relocate(el, cands, hwnd)
        if found is not None:
            return found
        tried = f"ct={node.get('ct')!r} name={node.get('name')[:24]!r}"
        raise BridgeError(
            f"元素 #{el.eid} 已失效：窗口 {hwnd} 的 UIA 树已重建，"
            f"按 RuntimeId 与语义({tried}) 都无法唯一定位，请重新 cu_show"
        )

    # -- actions ----------------------------------------------------------- #
    def act(
        self,
        hwnd: int,
        action: str,
        *,
        target: str | None = None,
        text: str | None = None,
        keys: str | None = None,
        value: str | None = None,
        amount: int = 0,
        at: str | None = None,
        to: str | None = None,
        timeout_ms: int = 3000,
        ms: int | None = None,
        button: str | None = None,
        hold_ms: int | None = None,
        **_ignored: Any,
    ) -> dict:
        t0 = time.perf_counter()
        action = (action or "").strip().lower()
        if not action:
            raise BridgeError("action 为空")
        hwnd = int(hwnd or 0)
        self.space_check()
        self._last_verified = None
        result = {"ok": False, "action": action, "via": None, "ms": 0, "detail": "", "changed": False}
        rehomed = 0

        try:
            # A UWP (ApplicationFrameHost) window can be re-created with a new
            # hwnd while ids/actions are in flight: relocate before failing.
            if hwnd and not _win.is_window(hwnd):
                new_hwnd = self._rehome(hwnd)
                if new_hwnd:
                    rehomed, hwnd = hwnd, new_hwnd
                elif action != "launch":
                    raise BridgeError(
                        f"窗口 {hwnd} 已关闭（按标题/进程也找不到替身窗口），请重新 cu_show"
                    )
            result["hwnd"] = hwnd

            if action in ("activate", "launch", "close", "minimize", "maximize", "restore"):
                via, detail, changed = self._window_action(hwnd, action, text or target)
                if action == "launch" and self._last_launch:
                    launched = int(self._last_launch.get("hwnd") or 0)
                    if launched:
                        result["hwnd"] = launched
                if rehomed:
                    detail = f"{detail} rehomed=#{hwnd}(原 #{rehomed})"
                result.update(ok=changed, via=via, detail=detail, changed=changed)
                return self._finish(result, t0)

            if action in ("wait", "assert"):
                if action == "assert":
                    w = self.wait(
                        "text", text=text or value, hwnd=hwnd, timeout_ms=timeout_ms
                    )
                    result.update(
                        ok=w["ok"],
                        via="wait",
                        detail=w["detail"],
                        ms=w["ms"],
                        changed=False,
                    )
                    return result
                kind = (text or "change").strip().lower()
                w = self.wait(
                    kind,
                    target=target,
                    text=value or text,
                    hwnd=hwnd,
                    timeout_ms=timeout_ms,
                )
                result.update(ok=w["ok"], via="uia", detail=w["detail"], ms=w["ms"])
                return result

            # everything below needs a target (or uses 'at' for points)
            el: Element | None = None
            resolved: dict = {}
            if at and action in ("click", "dblclick", "rightclick", "hover", "drag", "scroll"):
                x, y = self._point_from_spec(at)
                resolved = {"via": "point", "point": [x, y], "node": {}, "candidates": []}
            elif target:
                el, resolved = self.resolve(target, hwnd)
                el.hwnd = hwnd
            elif action in ("type", "key", "paste", "hold", "hold_mouse"):
                # 这些动作作用于当前焦点/光标，不需要元素 target
                resolved = {"via": "uia", "node": {}, "candidates": []}
            else:
                raise BridgeError(f"动作 {action} 需要 target")

            guard = self._locked() if action in LOCK_ACTIONS else contextlib.nullcontext()
            with guard:
                via, detail, changed = self._run_action(
                    hwnd, action, el, resolved, text, keys, value, amount, to,
                    ms=ms, button=button, hold_ms=hold_ms,
                )
            if resolved.get("relocated"):
                detail = f"{detail} {resolved['relocated']}"
            if rehomed:
                detail = f"{detail} rehomed=#{hwnd}(原 #{rehomed})"
            result.update(ok=True, via=via, detail=detail, changed=changed)
            if action in ("setvalue", "type", "paste"):
                result["verified"] = self._last_verified
        except AmbiguousError as exc:
            result.update(
                ok=False,
                via=None,
                detail=str(exc),
                changed=False,
                candidates=exc.candidates,
            )
        except BridgeError as exc:
            result.update(ok=False, via=None, detail=str(exc), changed=False)
        except GuiBusy as exc:
            result.update(ok=False, via=None, detail=f"桌面被其他任务占用: {exc}", changed=False)
        except Exception as exc:  # COM hiccups: report, never crash the server
            result.update(
                ok=False, via=None, detail=f"{type(exc).__name__}: {exc}", changed=False
            )
        return self._finish(result, t0)

    @staticmethod
    def _finish(result: dict, t0: float) -> dict:
        result["ms"] = int(round((time.perf_counter() - t0) * 1000))
        if not result.get("detail"):
            result["detail"] = f"{result.get('via')} ok" if result.get("ok") else "失败"
        return result

    def _window_action(self, hwnd: int, action: str, arg: str | None) -> tuple[str, str, bool]:
        if action == "launch":
            app = (arg or "").strip()
            if not app:
                raise BridgeError("launch 需要 app/文本参数（如 notepad.exe）")
            with self._locked():
                info = self._launch(app, exclude_hwnd=hwnd)
            self._last_launch = dict(info)
            matched = bool(info.get("matched"))
            h = info.get("hwnd") or 0
            base = (
                f"{info.get('proc') or app} #{h} pid={info.get('pid')} "
                f"title={(info.get('title') or '')[:40]!r} match={info.get('match')}"
            )
            if matched:
                return "win32", f"已启动 {base}", True
            if h:
                return (
                    "win32",
                    f"未检测到新窗口（应用可能复用已有窗口/启动较慢）；"
                    f"该应用已有窗口 {base}，请确认是不是它",
                    False,
                )
            return (
                "win32",
                f"未检测到新窗口（应用可能复用已有窗口/启动较慢）；"
                f"没找到属于 {app} 的窗口 pid={info.get('pid')}",
                False,
            )
        if not hwnd or not _user32.IsWindow(wt.HWND(hwnd)):
            raise BridgeError(f"窗口 {hwnd} 不存在")
        with self._locked():
            if action == "activate":
                st = _win.activate_ex(hwnd, wait_ms=150)
                if st.get("ok"):
                    notes = []
                    if st.get("was_minimized"):
                        notes.append("已从最小化还原")
                    if st.get("was_hidden"):
                        notes.append("已从隐藏(托盘)显示")
                    suffix = f"（{'，'.join(notes)}）" if notes else ""
                    return "win32", f"已激活{suffix}", True
                return "win32", f"激活失败：{st.get('reason') or '未知原因'}", False
            if action == "close":
                # UIA WindowPattern first (background, no focus change), then
                # WM_CLOSE; report honestly whether the window really went away.
                try:
                    pat = self._pattern(self._control_for(hwnd), "GetWindowPattern")
                    if pat is not None:
                        pat.Close()
                except Exception:
                    pass
                if not _wait_gone(hwnd, 700):
                    _win.close_window(hwnd)
                if _wait_gone(hwnd, 1600):
                    return "win32", "已关闭", True
                dlg = _modal_children(hwnd)
                if dlg:
                    return (
                        "win32",
                        f"已发送关闭请求，但窗口未关闭：弹出了对话框 #{dlg}（多半是保存确认），"
                        f"请先 cu_show 该对话框并处理",
                        False,
                    )
                return "win32", "已发送关闭请求，但 2.3s 后窗口仍在", False
            if action == "minimize":
                _win.show_window(hwnd, _win.SW_MINIMIZE)
                return "win32", "已最小化", True
            if action == "maximize":
                _win.show_window(hwnd, _win.SW_SHOWMAXIMIZED)
                return "win32", "已最大化", True
            _win.show_window(hwnd, _win.SW_RESTORE)
            return "win32", "已还原", True

    def _launch(self, app: str, exclude_hwnd: int = 0) -> dict:
        """Launch *app* and locate **its** window; never return someone else's.

        Matching, in order of confidence:
          1. pid of the process we spawned
          2. process image name == the app stem
          3. a brand new UWP frame window (ApplicationFrameHost) — calc.exe etc.
             appear this way, and only when no better candidate exists
        Never uses the foreground window, never falls back to "some window".
        Returns ``matched=False`` + the app's *existing* window when nothing new
        shows up (single-instance apps), so the caller can decide.
        """
        before_all = _win.list_windows()
        before = {w["hwnd"] for w in before_all}
        argv = shlex.split(app, posix=False)
        pid = 0
        try:
            pid = subprocess.Popen(argv, shell=False).pid
        except Exception:
            try:
                pid = subprocess.Popen(app, shell=True).pid
            except Exception as exc:
                raise BridgeError(f"启动失败: {app!r}: {exc}") from exc
        exe = argv[0].strip('"') if argv else app
        stem = os.path.splitext(os.path.basename(exe))[0].lower()
        # windows the same app already had (single-instance reuse)
        old_same = [
            w
            for w in before_all
            if w["hwnd"] != exclude_hwnd
            and (
                (pid and w["pid"] == pid)
                or (stem and stem in w["proc"].lower())
            )
        ]
        deadline = time.monotonic() + 5.0
        best: dict | None = None
        how = ""
        while time.monotonic() < deadline:
            new = [
                w
                for w in _win.list_windows()
                if w["hwnd"] not in before
                and w["hwnd"] != exclude_hwnd
                and (w["title"] or w["class"])
            ]
            if new:
                by_pid = [w for w in new if pid and w["pid"] == pid]
                by_name = [w for w in new if stem and w["proc"].lower() == stem]
                frames = [w for w in new if _is_uwp_frame(w)]
                if by_pid:
                    best, how = by_pid[0], "pid"
                elif by_name:
                    best, how = by_name[0], "proc-name"
                elif frames:
                    best, how = max(frames, key=lambda w: _area(w["rect"])), "uwp-frame"
            if best is not None:
                break
            time.sleep(0.1)

        if best is None:
            # single-instance app: report its real, pre-existing window
            reuse = sorted(old_same, key=lambda w: -_area(w["rect"]))
            if reuse:
                w = reuse[0]
                self._remember_window(w)
                return {**w, "match": "existing", "launched_pid": pid, "matched": False}
            return {
                "hwnd": 0,
                "pid": pid,
                "proc": "",
                "title": "",
                "class": "",
                "rect": [0, 0, 0, 0],
                "match": "none",
                "launched_pid": pid,
                "matched": False,
            }
        best = {**best, "match": how, "launched_pid": pid, "matched": True}
        self._remember_window(best)
        self._drop_window(int(best["hwnd"]))
        _win.activate(best["hwnd"], wait_ms=200)
        return best

    # -- individual actions ------------------------------------------------ #
    def _run_action(
        self,
        hwnd: int,
        action: str,
        el: Element | None,
        resolved: dict,
        text: str | None,
        keys: str | None,
        value: str | None,
        amount: int,
        to: str | None,
        *,
        ms: int | None = None,
        button: str | None = None,
        hold_ms: int | None = None,
    ) -> tuple[str, str, bool]:
        ctl = el.control if el is not None else None
        label = self._el_label(el, resolved)
        if action in _CLICKISH:
            self._require_enabled(ctl, label, action)

        if action == "invoke":
            pat = self._pattern(ctl, "GetInvokePattern")
            if pat is None:
                raise BridgeError(
                    f"元素 {label} 不支持 InvokePattern（pattern_unavailable）；"
                    f"请改用 click，或换一个可操作目标"
                )
            pat.Invoke()
            return "invoke", "InvokePattern", True

        if action == "toggle":
            pat = self._pattern(ctl, "GetTogglePattern")
            if pat is not None:
                pat.Toggle()
                return "toggle", "TogglePattern", True
            return (
                self._click_element(hwnd, el, resolved, clicks=1)[0],
                f"click 回退（toggle pattern_unavailable）",
                True,
            )

        if action == "select":
            pat = self._pattern(ctl, "GetSelectionItemPattern")
            if pat is not None:
                pat.Select()
                return "select", "SelectionItemPattern", True
            return (
                self._click_element(hwnd, el, resolved, clicks=1)[0],
                "click 回退（select pattern_unavailable）",
                True,
            )

        if action in ("expand", "collapse"):
            pat = self._pattern(ctl, "GetExpandCollapsePattern")
            if pat is not None:
                if action == "expand":
                    pat.Expand()
                else:
                    pat.Collapse()
                return action, f"ExpandCollapsePattern.{action.capitalize()}", True
            return (
                self._click_element(hwnd, el, resolved, clicks=1)[0],
                f"click 回退（{action} pattern_unavailable）",
                True,
            )

        if action == "focus":
            if ctl is None:
                raise BridgeError("focus 需要元素 target")
            ctl.SetFocus()
            return "uia", "SetFocus", True

        if action == "setvalue":
            if ctl is None:
                raise BridgeError("setvalue 需要元素 target")
            new = value if value is not None else (text or "")
            return self._set_value(ctl, new)

        if action in ("click", "dblclick", "rightclick", "hover"):
            clicks = 2 if action == "dblclick" else 1
            button = "right" if action == "rightclick" else "left"
            return self._click_element(
                hwnd, el, resolved, clicks=clicks, button=button, hover=(action == "hover")
            )

        if action == "type":
            content = text if text is not None else (value or "")
            return self._type_text(hwnd, ctl, content)

        if action == "paste":
            content = text if text is not None else (value or "")
            return self._paste(hwnd, content)

        if action == "key":
            if not keys:
                raise BridgeError("key 需要 keys 参数")
            self._ensure_focus(hwnd)
            desc = send_keys(keys)
            return "keys", f"SendKeys {desc}", True

        if action == "hold":
            # 按住键 N 毫秒：Minecraft 这类按 tick 采样"按住状态"的应用需要它
            if not keys:
                raise BridgeError("hold 需要 keys（如 w / shift+w），不支持空格分隔的按键序列")
            want, used, clipped = _clamp_hold_ms(ms)
            self._ensure_focus(hwnd)
            desc = hold_keys(keys, used)
            note = f"（ms 已截断到 {used}）" if clipped else ""
            return "keys", f"hold: {desc} {used}ms{note}", True

        if action == "hold_mouse":
            want, used, clipped = _clamp_hold_ms(ms)
            btn = (button or "left").strip().lower()
            self._ensure_focus(hwnd)
            info = hold_mouse(btn, used)
            note = f"（ms 已截断到 {used}）" if clipped else ""
            return "mouse", f"hold_mouse: {btn} {used}ms beats={info['beats']}{note}", True

        if action == "scroll_to":
            if ctl is None:
                raise BridgeError("scroll_to 需要元素 target")
            return self._scroll_into_view(ctl)

        if action == "scroll":
            return self._scroll(hwnd, el, resolved, amount)

        if action == "drag":
            return self._drag(hwnd, el, resolved, to, hold_ms=hold_ms)

        raise BridgeError(f"不支持的动作: {action}")

    @staticmethod
    def _pattern(ctl: Any, getter: str) -> Any:
        if ctl is None:
            return None
        try:
            fn = getattr(ctl, getter)
            return fn()
        except Exception:
            return None

    @staticmethod
    def _el_label(el: Element | None, resolved: dict) -> str:
        if el is not None:
            node = el.node or {}
            name = (node.get("name") or "").strip().replace("\n", " ")
            ct = node.get("ct") or "?"
            if el.eid:
                return f"'#{el.eid} {ct} {name[:24]}'"
            return f"'{ct} {name[:24]}'".strip()
        if resolved.get("via") == "point":
            p = resolved.get("point") or ()
            return f"坐标 {tuple(p)}"
        return "目标"

    @staticmethod
    def _require_enabled(ctl: Any, label: str, action: str) -> None:
        """Refuse to act on a disabled element instead of silently doing nothing."""
        if ctl is None:
            return
        enabled = _prop(ctl, "IsEnabled", True)
        if enabled is False:
            verb = {
                "click": "点击",
                "dblclick": "双击",
                "rightclick": "右键点击",
                "invoke": "invoke",
                "toggle": "toggle",
                "select": "选择",
                "expand": "展开",
                "collapse": "折叠",
                "hover": "悬停",
            }.get(action, action)
            raise BridgeError(
                f"元素 {label} 处于禁用状态（disabled），{verb} 不会生效；"
                f"请先让它可用或换路径"
            )

    def _ensure_focus(self, hwnd: int) -> None:
        if _win.foreground_hwnd() != int(hwnd):
            if not _win.activate(int(hwnd), wait_ms=200):
                raise BridgeError(f"无法把窗口 {hwnd} 激活到前台")

    def _fresh_center(self, el: Element) -> tuple[int, int]:
        ctl = el.control
        rect = _rect_tuple(_prop(ctl, "BoundingRectangle", None))
        if _area(rect) == 0:
            rect = list(el.node.get("rect") or [0, 0, 0, 0])
        offscreen = _prop(ctl, "IsOffscreen", False)
        if offscreen:
            pat = self._pattern(ctl, "GetScrollItemPattern")
            if pat is not None:
                try:
                    pat.ScrollIntoView()
                    time.sleep(0.05)
                    rect = _rect_tuple(_prop(ctl, "BoundingRectangle", None))
                except Exception:
                    pass
        if _area(rect) == 0:
            raise BridgeError(f"元素 #{el.eid} 没有可见矩形（offscreen），拒绝盲点")
        sx, sy = _win.screen_size()
        cx = min(max((rect[0] + rect[2]) // 2, 0), sx - 1)
        cy = min(max((rect[1] + rect[3]) // 2, 0), sy - 1)
        return cx, cy

    def _click_element(
        self,
        hwnd: int,
        el: Element | None,
        resolved: dict,
        *,
        clicks: int = 1,
        button: str = "left",
        hover: bool = False,
    ) -> tuple[str, str, bool]:
        if resolved.get("via") == "point":
            x, y = resolved["point"]
        else:
            if el is None:
                raise BridgeError("点击需要元素 target 或 at 坐标")
            x, y = self._fresh_center(el)
        if hover:
            _move_cursor(x, y)
            return "click", f"移动到 ({x},{y})", False
        self._ensure_focus(hwnd)
        _move_cursor(x, y)
        time.sleep(0.012)
        for i in range(max(1, clicks)):
            _mouse(button, True)
            time.sleep(0.008)
            _mouse(button, False)
            if i + 1 < clicks:
                time.sleep(0.06)
        return "click", f"{button} click x{clicks} @({x},{y})", True

    # -- value writing / typing -------------------------------------------- #
    def _read_back_value(self, ctl: Any) -> str | None:
        """Current value through ValuePattern (None = cannot read)."""
        pat = self._pattern(ctl, "GetValuePattern")
        if pat is None:
            return None
        try:
            v = pat.Value
        except Exception:
            return None
        return v

    def _verify(self, ctl: Any, expected: str) -> bool | None:
        """True/False from a read-back, None when the control cannot be read."""
        got = self._read_back_value(ctl)
        if got is None:
            return None
        return (got or "") == (expected or "")

    def _paste_via_clipboard(self, content: str) -> bool:
        """Clipboard + Ctrl+V with read-back; False means clipboard unusable."""
        if not _clipboard_write_verified(content, attempts=3):
            return False
        send_keys("ctrl+v")
        time.sleep(0.05)
        return True

    @staticmethod
    def plan_chunks(text: str, chunk: int = 40) -> list[str | None]:
        """Split text for SendKeys-style typing: >=40 chars, None == ENTER."""
        plan: list[str | None] = []
        buf = ""
        for ch in text or "":
            if ch == "\n":
                if buf:
                    plan.append(buf)
                    buf = ""
                plan.append(None)  # {ENTER}
                continue
            buf += ch
            if len(buf) >= chunk:
                plan.append(buf)
                buf = ""
        if buf:
            plan.append(buf)
        return plan

    def _type_by_chunks(self, text: str) -> int:
        """Type text without the clipboard (KEYEVENTF_UNICODE, ENTER for \\n)."""
        plan = self.plan_chunks(text)
        for i, part in enumerate(plan):
            if part is None:
                send_keys("enter")
            else:
                type_unicode(part)
            if i + 1 < len(plan):
                time.sleep(0.02 + random.random() * 0.02)  # 20-40ms jitter
        return len([p for p in plan if p is not None])

    def _set_value(self, ctl: Any, new: str) -> tuple[str, str, bool]:
        new = new or ""
        pat = self._pattern(ctl, "GetValuePattern")
        if pat is not None:
            try:
                pat.SetValue(new)
            except Exception:
                pass
            else:
                verified = self._verify(ctl, new)
                if verified is False:
                    got = self._read_back_value(ctl)
                    self._last_verified = False
                    raise BridgeError(
                        f"setvalue 回读不一致：写入 {new[:40]!r}，控件读回 {str(got)[:40]!r}"
                        f"（控件可能拒绝了这个值）"
                    )
                self._last_verified = verified
                suffix = "verified" if verified else "（该控件不支持回读）"
                return "setvalue", f"ValuePattern.SetValue({len(new)} chars) {suffix}", True
        # fallback: focus + Ctrl+A + paste/typing (needs the desktop lock)
        with self._locked():
            try:
                ctl.SetFocus()
            except Exception:
                pass
            send_keys("ctrl+a")
            time.sleep(0.02)
            if self._paste_via_clipboard(new):
                via, detail = "paste", f"焦点+Ctrl+A+粘贴({len(new)} chars)"
            else:
                chunks = self._type_by_chunks(new)
                via, detail = "sendkeys", f"剪贴板不可用，SendKeys 分块输入 chunks={chunks}"
            time.sleep(0.03)
            verified = self._verify(ctl, new)
        if verified is False:
            got = self._read_back_value(ctl)
            self._last_verified = False
            raise BridgeError(
                f"setvalue 回读不一致：写入 {new[:40]!r}，控件读回 {str(got)[:40]!r}"
            )
        self._last_verified = verified
        return via, detail, True

    def _type_text(self, hwnd: int, ctl: Any, content: str) -> tuple[str, str, bool]:
        if ctl is not None:
            pat = self._pattern(ctl, "GetValuePattern")
            if pat is not None and not _prop(ctl, "IsReadOnly", False):
                try:
                    pat.SetValue(content)
                except Exception:
                    pass
                else:
                    verified = self._verify(ctl, content)
                    if verified is not False:
                        self._last_verified = verified
                        return (
                            "setvalue",
                            f"ValuePattern.SetValue({len(content)} chars)"
                            + (" verified" if verified else ""),
                            True,
                        )
        self._ensure_focus(hwnd)
        if ctl is not None:
            try:
                ctl.SetFocus()
            except Exception:
                pass
            # an Edit is a "replace me" field: clear it before typing so a
            # prefilled name (e.g. save dialog) is not appended to.
            if _prop(ctl, "ControlTypeName", "") == "EditControl":
                send_keys("ctrl+a")
                time.sleep(0.015)
        if len(content) >= 48:
            with _clipboard_guard():
                if self._paste_via_clipboard(content):
                    verified = self._verify(ctl, content) if ctl is not None else None
                    self._last_verified = verified
                    detail = f"粘贴 {len(content)} 字符（剪贴板已回读校验）"
                    if verified is False:
                        raise BridgeError(
                            f"type 回读不一致：写入 {content[:40]!r}，控件读回 "
                            f"{str(self._read_back_value(ctl))[:40]!r}"
                        )
                    return "paste", detail, True
            chunks = self._type_by_chunks(content)
            self._last_verified = self._verify(ctl, content) if ctl is not None else None
            return "sendkeys", f"剪贴板不可用，SendKeys 分块输入 chunks={chunks}", True
        type_unicode(content)
        self._last_verified = self._verify(ctl, content) if ctl is not None else None
        return "keys", f"键入 {len(content)} 字符", True

    def _paste(self, hwnd: int, content: str) -> tuple[str, str, bool]:
        """Paste must never leak the user's previous clipboard content."""
        self._ensure_focus(hwnd)
        with _clipboard_guard():
            if self._paste_via_clipboard(content):
                return "paste", f"粘贴 {len(content)} 字符（剪贴板已回读校验）", True
            chunks = self._type_by_chunks(content)
        if chunks == 0 and content:
            raise ClipboardBusy(
                "剪贴板被其他程序占用（OpenClipboard 连续失败），SendKeys 兜底也没能输入"
            )
        return "sendkeys", f"剪贴板不可用，改用 SendKeys 分块输入 chunks={chunks}", True

    def _scroll_into_view(self, ctl: Any) -> tuple[str, str, bool]:
        """Scroll an element into view: ScrollItemPattern first, parent scroll after.

        Degrades to an **explicit failure** when neither pattern exists — it never
        pretends to have scrolled.
        """
        off_before = bool(_prop(ctl, "IsOffscreen", False))
        pat = self._pattern(ctl, "GetScrollItemPattern")
        if pat is not None:
            try:
                pat.ScrollIntoView()
                time.sleep(0.15)
                off_after = bool(_prop(ctl, "IsOffscreen", False))
                return (
                    "uia",
                    f"ScrollItemPattern.ScrollIntoView（offscreen {off_before}→{off_after}）",
                    off_before and not off_after,
                )
            except Exception:
                pass
        parent = ctl
        parent_pat = None
        for _ in range(4):
            try:
                parent = parent.GetParentControl()
            except Exception:
                parent = None
            if parent is None:
                break
            parent_pat = self._pattern(parent, "GetScrollPattern")
            if parent_pat is not None:
                break
        if parent_pat is None:
            raise BridgeError(
                "该元素不支持 ScrollIntoView，父容器也没有 ScrollPattern"
                "（ScrollItemPattern/ScrollPattern 都不可用），无法滚动到视口"
            )
        plain = auto.ScrollAmount.NoAmount
        step = auto.ScrollAmount.SmallIncrement
        try:
            for _ in range(6):
                parent_pat.Scroll(plain, step)
                time.sleep(0.08)
                if not _prop(ctl, "IsOffscreen", True):
                    return "uia", "父容器 ScrollPattern 滚动后已进入视口", True
        except Exception as exc:
            raise BridgeError(f"父容器滚动失败：{type(exc).__name__}: {exc}") from exc
        raise BridgeError(
            "ScrollIntoView 不可用，父容器滚动 6 次后元素仍不可见（该控件不支持滚动到视口）"
        )

    def _point_or_element(self, spec: str, hwnd: int) -> tuple[int, int]:
        """拖放终点：@x,y / @n: / @img: 或元素目标（#id / ct:name）。"""
        s = (spec or "").strip()
        if not s:
            raise BridgeError("拖放终点为空")
        m = re.match(r"^([A-Za-z]+):", s)
        is_selector = s.startswith("#") or bool(m and m.group(1).lower() in SHORT_CT)
        if not is_selector:
            return self._point_from_spec(s)
        el, _meta = self.resolve(s, hwnd)
        rect = _rect_tuple(_prop(el.control, "BoundingRectangle", None))
        if _area(rect) == 0:
            rect = list(el.node.get("rect") or [0, 0, 0, 0])
        if _area(rect) == 0:
            raise BridgeError(f"拖放终点 {s!r} 没有可见矩形（offscreen），无法作为落点")
        return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2

    # -- clipboard --------------------------------------------------------- #
    def clipboard_get(self) -> str | None:
        """Current clipboard text (None = no text / unreadable)."""
        return _win.get_clipboard_text()

    def clipboard_set(self, text: str, *, verify: bool = True) -> bool:
        """Write CF_UNICODETEXT with retries + read-back; ClipboardBusy on failure."""
        text = text or ""
        ok = _clipboard_write_verified(text) if verify else _win.set_clipboard_text(text)
        if not ok:
            raise ClipboardBusy(
                "剪贴板被其他程序占用（OpenClipboard 重试 3 次失败或回读不一致）"
            )
        return True

    # -- window geometry --------------------------------------------------- #
    def window_move(
        self, hwnd: int, x: int, y: int, w: int | None = None, h: int | None = None
    ) -> list[int]:
        """Move/resize (clamped to the virtual screen); returns the new rect."""
        hwnd = int(hwnd)
        if not _win.is_window(hwnd):
            raise BridgeError(f"窗口 #{hwnd} 已关闭，无法移动")
        with self._locked():
            rect = _win.move_window(hwnd, x, y, w, h)
        self._drop_window(hwnd)  # 位置变了，缓存 rect 已过期
        return rect

    def _nodes_for_marks(self, hwnd: int | None) -> list[dict]:
        """Nodes for Set-of-Mark: fresh snapshot cache, else one snapshot."""
        if not hwnd:
            return []
        cache = self._cache.get(int(hwnd))
        if cache and (time.monotonic() - float(cache.get("ts") or 0)) < 2.0:
            return list(cache.get("nodes") or [])
        try:
            return list(self.snapshot(int(hwnd), view="controls", max_nodes=300)["nodes"])
        except BridgeError:
            return []

    def _scroll(
        self, hwnd: int, el: Element | None, resolved: dict, amount: int
    ) -> tuple[str, str, bool]:
        n = int(amount) if amount else 3
        if el is not None:
            pat = self._pattern(el.control, "GetScrollPattern")
            if pat is not None:
                try:
                    step = auto.ScrollAmount.SmallIncrement
                    plain = auto.ScrollAmount.NoAmount
                    for _ in range(abs(n)):
                        if n > 0:
                            pat.Scroll(plain, step)
                        else:
                            pat.Scroll(plain, auto.ScrollAmount.SmallDecrement)
                    return "scroll", f"ScrollPattern {n}", True
                except Exception:
                    pass
        if resolved.get("via") == "point":
            x, y = resolved["point"]
        elif el is not None:
            x, y = self._fresh_center(el)
        else:
            r = _win.get_rect(hwnd)
            x, y = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        self._ensure_focus(hwnd)
        _wheel(x, y, -120 * int(n))
        return "click", f"滚轮 {n} 格 @({x},{y})", True

    def _drag(
        self,
        hwnd: int,
        el: Element | None,
        resolved: dict,
        to: str | None,
        hold_ms: int | None = None,
    ) -> tuple[str, str, bool]:
        """Drag from the start point to *to*, then verify the window followed.

        A caption drag on a maximized window makes Windows do its own
        restore+snap dance, which makes the window lag the cursor on one axis
        (the reported "Δx = 0" bug).  We therefore restore the window first and
        afterwards compare the cursor delta with the real window delta; if they
        disagree we fail loudly instead of reporting a silent success.
        """
        if not to:
            raise BridgeError("drag 需要 to 目标坐标")
        tx, ty = self._point_or_element(to, hwnd)
        if resolved.get("via") == "point":
            x, y = resolved["point"]
        elif el is not None:
            x, y = self._fresh_center(el)
        else:
            raise BridgeError("drag 需要 target 或 at")
        win_before = _win.get_rect(hwnd) if hwnd and _win.is_window(hwnd) else None
        caption = self._looks_like_caption(hwnd, x, y)
        restored = False
        if caption and _win.is_maximized(hwnd):
            _win.show_window(hwnd, _win.SW_RESTORE)
            time.sleep(0.45)
            nr = _win.get_rect(hwnd)
            # the restored window is smaller and moved: re-derive the caption
            # grab point and shift the target by the same offset so the
            # intended cursor delta is preserved.
            nx = nr[0] + max(40, min((nr[2] - nr[0]) // 3, 300))
            ny = nr[1] + 12
            tx += nx - x
            ty += ny - y
            x, y = nx, ny
            win_before = nr
            restored = True
        self._ensure_focus(hwnd)
        _move_cursor(x, y)
        time.sleep(0.03)
        _mouse("left", True)
        time.sleep(0.06)
        if hold_ms:
            # 可选：按下后先保持 N 毫秒再移动（挖方块/长按拖拽类场景）
            extra = max(0, min(HOLD_MAX_MS, int(hold_ms))) / 1000.0
            if extra:
                time.sleep(extra)
                detail_hold = f" hold_ms={int(hold_ms)}"
            else:
                detail_hold = ""
        else:
            detail_hold = ""
        path = drag_path(x, y, tx, ty)
        for i, (px, py) in enumerate(path):
            # 标题栏拖动靠 Windows 的 modal move 循环：它只跟随真实光标位置，
            # SendInput 注入的绝对移动会让它把增量算成 0（实测窗口完全不动）。
            # 所以先 SetCursorPos（可靠驱动窗口移动）。
            _move_cursor(px, py)
            if not caption and i + 1 < len(path):
                # 内容拖动（canvas/游戏/拖放）额外发真实 MOUSEMOVE，便于按帧读输入的 app
                _move_cursor_abs(px, py)
            time.sleep(0.02)
        time.sleep(0.05)
        _mouse("left", False)
        time.sleep(0.08)
        detail = f"拖动 ({x},{y}) -> ({tx},{ty})" + locals().get("detail_hold", "")
        if not caption:
            # 内容拖动（选文本/滚动条/拖放/拖进输入框）：窗口本就不该跟动，只报事实
            detail += " drag content"
        if restored:
            detail += " 已先还原最大化窗口"
        if caption and win_before is not None:
            win_after = _win.get_rect(hwnd)
            wdx, wdy = win_after[0] - win_before[0], win_after[1] - win_before[1]
            cdx, cdy = tx - x, ty - y
            # 期望位移必须先按"屏幕夹取"算：窗口贴着虚拟屏边缘时，Windows 会把
            # 那一轴夹住（合法 0 位移），拿未夹取的原始 delta 比会误判。
            try:
                vx, vy, vw, vh = _win.virtual_screen()
                win_w = win_before[2] - win_before[0]
                win_h = win_before[3] - win_before[1]
                exp_dx = _clamp_int(win_before[0] + cdx, vx, vx + vw - win_w) - win_before[0]
                exp_dy = _clamp_int(win_before[1] + cdy, vy, vy + vh - win_h) - win_before[1]
            except Exception:
                exp_dx, exp_dy = cdx, cdy
            detail += f" window_delta=({wdx},{wdy}) exp_wdelta=({exp_dx},{exp_dy})"

            def _axis_ok(actual: int, expected: int, raw: int) -> bool:
                # 夹取时 expected 才是对的；没夹取时 raw 也对（OS 允许窗口越界）
                return abs(actual - expected) <= 5 or abs(actual - raw) <= 5

            if not (_axis_ok(wdx, exp_dx, cdx) and _axis_ok(wdy, exp_dy, cdy)):
                raise BridgeError(
                    f"{detail}；但窗口位移 ({wdx},{wdy}) 与期望 ({exp_dx},{exp_dy}) /"
                    f"光标位移 ({cdx},{cdy}) 都不符"
                    f"（起点可能不在可拖动区域，或窗口被应用限制），拖动没有生效"
                )
        return "click", detail, True

    @staticmethod
    def _looks_like_caption(hwnd: int, x: int, y: int) -> bool:
        """Is (x,y) on the *title bar* (the only place a window should follow)?

        Two gates, because the window-delta check is strict and must not fire for
        content drags (selecting text, dragging a scrollbar, dropping a file):

        1. geometry: inside the non-client caption band (``caption_height``);
           borderless windows (Chromium) get a small 24px band instead of a
           height-proportional one;
        2. content probe: the UIA element under the point must not be page/editor
           content (document, edit, list, tab, toolbar…); only the window itself
           or a titlebar element counts as a caption.
        """
        if not hwnd or not _win.is_window(hwnd):
            return False
        rect = _win.get_rect(hwnd)
        if not (rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]):
            return False
        try:
            cap_h = int(_win.caption_height(hwnd))
        except Exception:
            cap_h = 0
        if _win.is_maximized(hwnd):
            band = max(24, min(72, cap_h + 8))
        elif cap_h > 0:
            band = min(72, cap_h)
        else:
            band = 24  # 自绘标题栏：保守给一小条
        if not (0 <= y - rect[1] <= band):
            return False
        # 二次保护：点在内容元素上就不算标题栏
        try:
            ctl = auto.ControlFromPoint(int(x), int(y))
        except Exception:
            return True
        for _ in range(6):
            if ctl is None:
                return True
            ct = _prop(ctl, "ControlTypeName", "") or ""
            if ct in ("WindowControl", "TitleBarControl"):
                return True
            if ct in _CAPTION_BLOCK_CTS:
                return False
            try:
                ctl = ctl.GetParentControl()
            except Exception:
                return True
        return True

    # -- screenshot -------------------------------------------------------- #
    def screenshot(
        self,
        hwnd: int | None = None,
        *,
        region: list[int] | None = None,
        max_width: int = 1280,
        quality: int = 70,
        marks: bool = False,
        marks_max: int = 120,
    ) -> dict:
        from PIL import Image, ImageGrab  # lazy: importing bridge must not load Pillow

        t0 = time.perf_counter()
        self.space_check()
        vx, vy, vw, vh = _win.virtual_screen()
        if region is not None:
            if len(region) != 4:
                raise BridgeError("region 需要 [x, y, w, h]")
            x, y, w, h = [int(v) for v in region]
            box = (x, y, x + w, y + h)
        elif hwnd:
            rect = _win.get_rect(int(hwnd))
            box = (rect[0], rect[1], rect[2], rect[3])
        else:
            box = None

        if box is not None:
            l, t, r, b = box
            cl = min(max(l, vx), vx + vw - 1)
            ct = min(max(t, vy), vy + vh - 1)
            cr = min(max(r, cl + 1), vx + vw)
            cb = min(max(b, ct + 1), vy + vh)
            box = (cl, ct, cr, cb)
            crop_w, crop_h = cr - cl, cb - ct
            origin = [cl, ct]
        else:
            origin = [vx, vy]
            crop_w, crop_h = vw, vh
        try:
            img = ImageGrab.grab(bbox=box, all_screens=True)
        except TypeError:  # very old Pillow
            img = ImageGrab.grab(bbox=box)
        if img is None:
            raise BridgeError("截图失败：ImageGrab 返回 None")
        if abs(img.width - crop_w) > 2 or abs(img.height - crop_h) > 2:
            raise BridgeError(
                "截图尺寸与请求区域不一致 "
                f"(img={img.width}x{img.height}, 期望 {crop_w}x{crop_h})——坐标空间不一致"
            )
        screen_w, screen_h = crop_w, crop_h
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        if max_width and img.width > int(max_width):
            ratio = int(max_width) / img.width
            img = img.resize((int(max_width), max(1, int(round(img.height * ratio)))), Image.BILINEAR)
        scale = (screen_w / img.width) if img.width else 1.0
        mark_list: list[dict] = []
        if marks:
            crop = (origin[0], origin[1], origin[0] + screen_w, origin[1] + screen_h)
            mark_list = collect_marks(self._nodes_for_marks(hwnd), crop, limit=int(marks_max))
            if mark_list:
                img = annotate_marks(img, mark_list, origin, scale)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=int(quality), optimize=False, progressive=False)
        data = buf.getvalue()
        shot = {
            "mime": "image/jpeg",
            "jpeg_bytes": data,
            "img_w": img.width,
            "img_h": img.height,
            "screen_w": screen_w,
            "screen_h": screen_h,
            "screen": [screen_w, screen_h],
            "origin": origin,
            "scale": scale,
            "space": "physical",
            "ms": 0,
        }
        if marks:
            shot["marks"] = mark_list
        self._last_shot = shot
        shot["ms"] = int(round((time.perf_counter() - t0) * 1000))
        return shot

    def last_shot_map(self) -> dict | None:
        if not self._last_shot:
            return None
        s = self._last_shot
        return {
            "origin": list(s["origin"]),
            "scale": s["scale"],
            "img_w": s["img_w"],
            "img_h": s["img_h"],
            "screen_w": s["screen_w"],
            "screen_h": s["screen_h"],
        }

    # -- wait -------------------------------------------------------------- #
    def _names(self, hwnd: int, limit: int = 400) -> list[tuple[str, str]]:
        root = self._control_for(hwnd)
        out: list[tuple[str, str]] = []
        stack = [(root, 0)]
        n = 0
        while stack and n < limit:
            ctl, depth = stack.pop()
            n += 1
            ct = _prop(ctl, "ControlTypeName", "") or ""
            name = _prop(ctl, "Name", "") or ""
            out.append((ct, name))
            if ct in LEAF_TYPES or depth >= 24:
                continue
            try:
                kids = ctl.GetChildren() or []
            except Exception:
                kids = []
            for k in reversed(kids):
                stack.append((k, depth + 1))
        return out

    def wait(
        self,
        kind: str,
        *,
        target: str | None = None,
        text: str | None = None,
        value: str | None = None,
        hwnd: int | None = None,
        timeout_ms: int = 3000,
        interval_ms: int = 80,
    ) -> dict:
        t0 = time.perf_counter()
        kind = (kind or "").strip().lower()
        needle = text if text is not None else value
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        hwnd = int(hwnd) if hwnd else 0
        if not hwnd:
            hwnd = _win.foreground_hwnd()

        def done(ok: bool, detail: str) -> dict:
            return {
                "ok": ok,
                "ms": int(round((time.perf_counter() - t0) * 1000)),
                "detail": detail,
            }

        if kind == "window":
            if not needle:
                needle = target  # 兼容 target= 传法（server 两种都可能给）
            if not needle:
                raise BridgeError("wait window 需要标题子串（text 或 target）")
            while True:
                hits = _win.find_windows(needle, limit=1)
                if hits:
                    return done(True, f"窗口已出现 #{hits[0]['hwnd']} {hits[0]['title'][:40]!r}")
                if time.monotonic() >= deadline:
                    return done(False, f"{int(timeout_ms)}ms 内未等到窗口 {needle!r}")
                time.sleep(max(20, int(interval_ms)) / 1000.0)

        if kind in ("idle", "change"):
            try:
                base = self._names(hwnd)
            except BridgeError:
                return done(False, f"窗口 {hwnd} 不可访问")
            prev = tuple(base)
            while True:
                time.sleep(max(20, int(interval_ms)) / 1000.0)
                try:
                    cur = tuple(self._names(hwnd))
                except BridgeError:
                    return done(False, f"窗口 {hwnd} 已消失")
                if kind == "idle" and cur == prev:
                    return done(True, f"UIA 树稳定（{len(cur)} 节点）")
                if kind == "change" and cur != prev:
                    return done(True, f"UIA 树已变化（{len(cur)} 节点）")
                prev = cur
                if time.monotonic() >= deadline:
                    return done(False, f"{int(timeout_ms)}ms 内未满足 {kind}")

        if kind in ("text", "element", "gone"):
            if not needle and not target:
                raise BridgeError(f"wait {kind} 需要 text 或 target")
            nf = _norm(needle or "")
            while True:
                hit = False
                detail = ""
                if kind == "element":
                    try:
                        self.resolve(target, hwnd)
                        hit, detail = True, f"元素已出现: {target}"
                    except BridgeError:
                        hit, detail = False, f"元素尚未出现: {target}"
                elif kind == "gone":
                    try:
                        self.resolve(target, hwnd)
                        hit, detail = False, f"元素仍在: {target}"
                    except AmbiguousError:
                        hit, detail = False, f"元素仍在（歧义）: {target}"
                    except BridgeError:
                        hit, detail = True, f"元素已消失: {target}"
                else:
                    title = _norm(_win.get_title(hwnd))
                    if nf and nf in title:
                        hit, detail = True, f"标题包含 {needle!r}"
                    else:
                        for ct, name in self._names(hwnd):
                            if nf and nf in _norm(name):
                                hit, detail = True, f"元素 {ct} {name[:30]!r} 包含 {needle!r}"
                                break
                        if not hit:
                            detail = f"未见文本 {needle!r}"
                if hit:
                    return done(True, detail)
                if time.monotonic() >= deadline:
                    return done(False, f"{int(timeout_ms)}ms 超时：{detail}")
                time.sleep(max(20, int(interval_ms)) / 1000.0)

        raise BridgeError(f"未知 wait kind: {kind}")


def _safe_children(ctl: Any) -> list:
    try:
        return ctl.GetChildren() or []
    except Exception:
        return []


_UWP_FRAME_CLASSES = {
    "applicationframewindow",
    "windows.ui.core.corewindow",
    "modernwindow",
}


def _is_uwp_frame(w: dict) -> bool:
    """True for a UWP app's top-level window (ApplicationFrameHost hosts it)."""
    if not w.get("visible") or not (w.get("title") or "").strip():
        return False
    cls = (w.get("class") or "").lower()
    if cls in _UWP_FRAME_CLASSES:
        return True
    return (w.get("proc") or "").lower() == "applicationframehost"


def _wait_gone(hwnd: int, timeout_ms: int) -> bool:
    """True once the window handle disappears (or is no longer a window)."""
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
    while time.monotonic() < deadline:
        if not _user32.IsWindow(wt.HWND(int(hwnd))):
            return True
        time.sleep(0.04)
    return not bool(_user32.IsWindow(wt.HWND(int(hwnd))))


def _modal_children(hwnd: int) -> int | None:
    """hwnd of a dialog owned by *hwnd* (e.g. a save-confirmation popup)."""
    GW_OWNER = 4
    found: list[int] = []

    def _cb(h, _l):
        try:
            if int(_user32.GetWindow(wt.HWND(int(h)), GW_OWNER)) == int(hwnd):
                found.append(int(h))
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_win._EnumWindowsProc(_cb), 0)
    except Exception:
        return None
    for h in found:
        if _user32.IsWindowVisible(wt.HWND(h)):
            return h
    return None

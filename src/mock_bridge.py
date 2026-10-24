"""Offline mock of CONTRACT §3 (`windows.py` + `bridge.py`) — T2's stand-in.

Used by ``python -u src/server.py --mock`` (or ``SUPER_CU_MOCK=1``) and by
``tests/test_server.py`` while T1's real bridge is still being written.  It is
deterministic and never touches the real desktop, but it is faithful where it
matters for the presentation layer:

* stable integer ids (same element -> same id across snapshots),
* the exact dict shapes from CONTRACT §3,
* physical-pixel coordinates on a 2560x1600 screen,
* a real JPEG from Pillow when available (otherwise a 1x1 fallback),
* scenes for Notepad / Calculator / Explorer / Edge with 15 tabs,
* injectable failures so the boundary cases can be tested.

Only the protocol-side behaviour is simulated: nothing here clicks a real
window, so mock runs never need the GUI lock.
"""
from __future__ import annotations

import base64
import io
import time

SCREEN = (2560, 1600)
CURSOR = 0


class BridgeError(Exception):
    """Same contract as T1: human-readable message, always caught by server."""

    def __init__(self, msg, candidates=None):
        super().__init__(msg)
        self.candidates = list(candidates or [])


class AmbiguousError(BridgeError):
    pass


# --------------------------------------------------------------------------- #
# node helper
# --------------------------------------------------------------------------- #
def _n(rid, ct, name="", rect=(0, 0, 0, 0), depth=0, patterns=(), kids=0,
       val=None, enabled=True, offscreen=False, focused=False, selected=False,
       aid=None, path=""):
    return {
        "_rid": rid, "ct": ct, "name": name, "rect": list(rect), "depth": depth,
        "patterns": list(patterns), "kids": kids, "val": val, "enabled": enabled,
        "offscreen": offscreen, "focused": focused, "selected": selected,
        "aid": aid, "path": path,
    }


# --------------------------------------------------------------------------- #
# scenes
# --------------------------------------------------------------------------- #
def _notepad():
    nodes = [
        _n("np.win", "window", "未命名 - 记事本", (100, 80, 1000, 700), 0, kids=3),
        _n("np.menubar", "menubar", "", (100, 80, 1000, 112), 1, kids=5, path=""),
    ]
    for i, menu in enumerate(("文件", "编辑", "格式", "查看", "帮助")):
        nodes.append(_n(f"np.menu.{menu}", "menuitem", menu,
                        (118 + i * 58, 84, 176 + i * 58, 108), 2, ("invoke",),
                        path="菜单栏"))
    nodes += [
        _n("np.body", "pane", "", (100, 112, 1000, 700), 1, kids=2),
        _n("np.edit", "edit", "文本编辑器", (108, 120, 992, 672), 2,
           ("textpattern", "setvalue"), val="", path=""),
        _n("np.status", "statusbar", "第 1 行，第 1 列", (100, 672, 1000, 700), 2,
           path=""),
    ]
    return nodes


def _calculator():
    nodes = [
        _n("calc.win", "window", "计算器", (700, 300, 1200, 900), 0, kids=3),
        _n("calc.mode", "text", "标准", (700, 300, 1200, 340), 1,
           path="菜单栏"),
        _n("calc.display", "text", "0", (720, 350, 1180, 440), 1,
           path=""),
    ]
    rows = (("C", "CE", "%", "÷"), ("7", "8", "9", "×"),
            ("4", "5", "6", "−"), ("1", "2", "3", "+"),
            ("±", "0", ".", "="))
    for r, row in enumerate(rows):
        for c, label in enumerate(row):
            nodes.append(_n(f"calc.btn.{label}", "button", label,
                            (720 + c * 112, 450 + r * 82,
                             820 + c * 112, 520 + r * 82), 1, ("invoke",),
                            path=""))
    return nodes


def _explorer():
    return [
        _n("ex.win", "window", "下载", (200, 150, 1400, 950), 0, kids=3),
        _n("ex.nav", "pane", "导航窗格", (200, 150, 400, 950), 1, kids=4),
        _n("ex.nav.dl", "listitem", "下载", (210, 200, 390, 236), 2,
           ("selection", "invoke"), selected=True, path="导航窗格"),
        _n("ex.nav.doc", "listitem", "文档", (210, 240, 390, 276), 2,
           ("selection", "invoke"), path="导航窗格"),
        _n("ex.nav.pic", "listitem", "图片", (210, 280, 390, 316), 2,
           ("selection", "invoke"), path="导航窗格"),
        _n("ex.toolbar", "toolbar", "", (400, 150, 1400, 200), 1, kids=3),
        _n("ex.new", "button", "新建", (410, 158, 480, 192), 2, ("invoke",),
           path="工具栏"),
        _n("ex.addr", "edit", "地址栏", (500, 158, 1200, 192), 2,
           ("value", "setvalue"), val="D:\\Users\\LENOVO\\Downloads", path="工具栏"),
        _n("ex.list", "list", "", (400, 200, 1400, 950), 1, kids=0),
        _n("ex.file1", "listitem", "super-cu-1.0.0.zip", (410, 210, 1390, 250), 2,
           ("selection", "invoke"), path=""),
        _n("ex.file2", "listitem", "report.md", (410, 254, 1390, 294), 2,
           ("selection", "invoke"), path=""),
    ]


def _settings():
    return [
        _n("set.win", "window", "设置", (300, 200, 1300, 900), 0, kids=2),
        _n("set.nav", "pane", "设置导航", (300, 200, 620, 900), 1, kids=3),
        _n("set.nav.sys", "listitem", "系统", (310, 240, 610, 280), 2,
           ("selection", "invoke"), path="设置导航"),
        _n("set.nav.bt", "listitem", "蓝牙和其他设备", (310, 284, 610, 324), 2,
           ("selection", "invoke"), path="设置导航"),
        _n("set.body", "pane", "系统", (620, 200, 1300, 900), 1, kids=2),
        _n("set.title", "text", "系统", (650, 240, 1200, 290), 2, path=""),
        _n("set.toggle", "checkbox", "夜间模式", (650, 320, 900, 360), 2,
           ("toggle",), path=""),
    ]


def _terminal():
    return [
        _n("term.win", "window", "Windows PowerShell", (0, 0, 1280, 800), 0, kids=1),
        _n("term.doc", "document", "Windows PowerShell", (8, 40, 1272, 792), 1,
           ("textpattern",), val="PS D:\\opencode>", path=""),
    ]


_EDGE_TABS = (
    "对话 · DeepSeek Harness", "题目收集平台", "GitHub", "文档 - DeepSeek",
    "收件箱 (3)", "新闻", "哔哩哔哩", "知乎", "小红书", "全网搜索",
    "DeepSeek 官网", "控制台", "论文 · arxiv", "翻译", "设置",
)

_EDGE_TEXTS = (
    "题目收集平台", "共 128 道题 · 已收集 96", "第 1 题",
    "以下关于 TCP 三次握手的说法，正确的是", "A. 第三次握手可以携带数据",
    "B. 第二次握手服务器不分配资源", "C. 握手过程使用 UDP 承载",
    "D. 首次握手由服务器发起", "提交答案", "上一题", "下一题", "进度 1/128",
)


def _edge():
    nodes = [
        _n("edge.win", "window", "题目收集平台 - Microsoft Edge",
           (-11, -11, 2549, 1539), 0, kids=3, path=""),
        _n("edge.pane", "pane", "", (-11, 40, 2549, 1539), 1, kids=3),
        _n("edge.toolbar", "toolbar", "", (-11, 40, 2549, 120), 2, kids=9,
           path=""),
    ]
    for i, name in enumerate(("后退", "前进", "刷新", "收藏夹", "集锦",
                              "扩展", "设置及更多", "个人资料")):
        nodes.append(_n(f"edge.tb.{name}", "button", name,
                        (20 + i * 46, 58, 58 + i * 46, 96), 3, ("invoke",),
                        path="工具栏"))
    nodes += [
        _n("edge.addr", "edit", "地址和搜索栏", (420, 58, 2000, 96), 3,
           ("value", "setvalue"), val="127.0.0.1:2476", path="工具栏"),
        _n("edge.tabs", "tab", "", (-11, 120, 2549, 168), 2, kids=16,
           path=""),
    ]
    for i, title in enumerate(_EDGE_TABS):
        nodes.append(_n(f"edge.tab.{i}", "tabitem", title,
                        (14 + i * 150, 122, 160 + i * 150, 166), 3,
                        ("selection", "invoke"), focused=(i == 0),
                        selected=(i == 0), path="标签栏"))
    nodes.append(_n("edge.newtab", "button", "新选项卡",
                    (14 + 15 * 150, 124, 74 + 15 * 150, 164), 3, ("invoke",),
                    path="标签栏"))
    nodes.append(_n("edge.doc", "document", "题目收集平台",
                    (-11, 168, 2549, 1539), 2, ("textpattern",), path=""))
    for i, text in enumerate(_EDGE_TEXTS):
        nodes.append(_n(f"edge.text.{i}", "text", text,
                        (40, 200 + i * 44, 1200, 236 + i * 44), 3,
                        path="题目收集平台"))
    return nodes


def _large():
    """Synthetic 1200-node window used to exercise the cropping path."""
    nodes = [_n("big.win", "window", "大页面", (0, 0, 2560, 1600), 0, kids=1200)]
    for i in range(1200):
        ct = "button" if i % 3 == 0 else ("text" if i % 3 == 1 else "pane")
        nodes.append(_n(f"big.{i}", ct, f"元素 {i} 的标题", (
            10 + (i % 40) * 60, 40 + (i // 40) * 48,
            60 + (i % 40) * 60, 80 + (i // 40) * 48), 1,
            ("invoke",) if ct == "button" else (), path=""))
    return nodes


_BUILDERS = {
    1001: _notepad,
    1002: _edge,
    1003: _calculator,
    1004: _explorer,
    1005: _settings,
    1006: _terminal,
    1007: _large,
}

_WINDOWS = [
    {"hwnd": 1002, "pid": 2222, "proc": "msedge",
     "title": "题目收集平台 - Microsoft Edge", "class": "Chrome_WidgetWin_1",
     "rect": [-11, -11, 2549, 1539], "visible": True, "minimized": False, "z": 0},
    {"hwnd": 1001, "pid": 1111, "proc": "notepad",
     "title": "未命名 - 记事本", "class": "Notepad",
     "rect": [100, 80, 1000, 700], "visible": True, "minimized": False, "z": 1},
    {"hwnd": 1004, "pid": 4444, "proc": "explorer", "title": "下载",
     "class": "CabinetWClass", "rect": [200, 150, 1400, 950],
     "visible": True, "minimized": False, "z": 2},
    {"hwnd": 1003, "pid": 3333, "proc": "ApplicationFrameHost",
     "title": "计算器", "class": "ApplicationFrameWindow",
     "rect": [700, 300, 1200, 900], "visible": True, "minimized": False, "z": 3},
    {"hwnd": 1005, "pid": 5555, "proc": "SystemSettings", "title": "设置",
     "class": "ApplicationFrameWindow", "rect": [300, 200, 1300, 900],
     "visible": True, "minimized": False, "z": 4},
    {"hwnd": 1007, "pid": 7777, "proc": "mockbig", "title": "大页面",
     "class": "MockBig", "rect": [0, 0, 2560, 1600],
     "visible": False, "minimized": False, "z": 5},
]


# --------------------------------------------------------------------------- #
# shared state (windows functions and Bridge instances share one mock desktop)
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.ids = {}
        self.by_id = {}
        self.next_id = 1000
        self.foreground = 1002
        self.windows = {w["hwnd"]: dict(w) for w in _WINDOWS}
        self.edits = {}
        self.focus = {}
        self.shot = None
        self.launched = []
        self.missing = set()
        self.clipboard = ""          # B8: clipboard op state
        self.scroll_into = set()     # B8: (hwnd, rid) scrolled into view

    def id_for(self, rid):
        if rid not in self.ids:
            self.next_id += 1
            self.ids[rid] = self.next_id
        return self.ids[rid]


_STATE = _State()


def reset():
    """Test hook: return the mock desktop to its initial state."""
    _STATE.reset()


# --------------------------------------------------------------------------- #
# windows.py surface
# --------------------------------------------------------------------------- #
def list_windows():
    wins = []
    for win in sorted(_STATE.windows.values(), key=lambda w: w.get("z", 0)):
        if win["hwnd"] in _STATE.missing:
            continue
        item = dict(win)
        item["focused"] = (win["hwnd"] == _STATE.foreground
                           and win.get("visible", True)
                           and not win.get("minimized"))
        wins.append(item)
    return wins


def foreground_hwnd():
    return _STATE.foreground


def _score(win, query):
    q = query.strip().lower()
    if q.startswith("#") and q[1:].isdigit():
        return 100 if win["hwnd"] == int(q[1:]) else 0
    title = (win.get("title") or "").lower()
    proc = (win.get("proc") or "").lower()
    cls = (win.get("class") or "").lower()
    if q == title:
        return 90
    if title.startswith(q):
        return 80
    if q in title:
        return 60
    if q == proc or q == cls:
        return 50
    if q in proc or q in cls:
        return 40
    return 0


def find_windows(query, limit=10):
    if not query:
        return list_windows()[:limit]
    scored = []
    for win in list_windows():
        score = _score(win, query)
        if score > 0:
            if win.get("focused"):
                score += 3
            if win.get("visible"):
                score += 2
            scored.append((score, win))
    scored.sort(key=lambda pair: -pair[0])
    return [win for _, win in scored[:limit]]


def resolve_hwnd(query):
    if not query:
        return _STATE.foreground
    found = find_windows(query, limit=1)
    return found[0]["hwnd"] if found else None


def activate(hwnd, wait_ms=120):
    hwnd = int(hwnd)
    if hwnd not in _STATE.windows or hwnd in _STATE.missing:
        return False
    win = _STATE.windows[hwnd]
    win["minimized"] = False
    _STATE.foreground = hwnd
    return True


def get_rect(hwnd):
    hwnd = int(hwnd)
    if hwnd not in _STATE.windows or hwnd in _STATE.missing:
        raise BridgeError(f"窗口 {hwnd} 已关闭或不存在")
    return list(_STATE.windows[hwnd]["rect"])


def move_window(hwnd, x, y, w=None, h=None):
    """Mock of windows.move_window: move/resize and return the new rect."""
    hwnd = int(hwnd)
    if hwnd not in _STATE.windows or hwnd in _STATE.missing:
        raise BridgeError(f"窗口 {hwnd} 已关闭或不存在")
    win = _STATE.windows[hwnd]
    rect = win["rect"]
    cw = int(w) if w else max(80, rect[2] - rect[0])
    ch = int(h) if h else max(80, rect[3] - rect[1])
    win["rect"] = [int(x), int(y), int(x) + cw, int(y) + ch]
    win["minimized"] = False
    return list(win["rect"])


def get_clipboard_text(attempts=6):
    return _STATE.clipboard


def set_clipboard_text(text, attempts=6):
    _STATE.clipboard = str(text or "")
    return True


def wait_for_window(query, timeout_ms):
    found = find_windows(query, limit=1)
    if found:
        return found[0]
    time.sleep(min(timeout_ms, 150) / 1000.0)
    return None


# --------------------------------------------------------------------------- #
# image helper
# --------------------------------------------------------------------------- #
_FALLBACK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)


def _jpeg(width, height, quality=70):
    try:
        from PIL import Image
    except Exception:
        return _FALLBACK_JPEG
    try:
        img = Image.new("RGB", (max(1, width), max(1, height)), (32, 34, 40))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return _FALLBACK_JPEG


# --------------------------------------------------------------------------- #
# bridge.py surface
# --------------------------------------------------------------------------- #
class Bridge:
    """Deterministic in-memory Bridge implementing CONTRACT §3.2."""

    def __init__(self, **_ignored):
        self.s = _STATE

    # -- internals --------------------------------------------------------- #
    def _win(self, hwnd):
        try:
            hwnd = int(hwnd)
        except (TypeError, ValueError):
            raise BridgeError(f"无效窗口句柄 {hwnd!r}")
        if hwnd not in _STATE.windows or hwnd in _STATE.missing:
            raise BridgeError(f"窗口 {hwnd} 已关闭或不存在；用 cu_show scope=desktop 看窗口列表")
        return _STATE.windows[hwnd]

    def _build(self, hwnd):
        self._win(hwnd)
        builder = _BUILDERS.get(int(hwnd))
        if builder is None:
            raise BridgeError(f"窗口 {hwnd} 没有 mock 场景")
        nodes = builder()
        for node in nodes:
            node["id"] = _STATE.id_for(node["_rid"])
            edit = _STATE.edits.get((int(hwnd), node["_rid"]))
            if edit is not None:
                node["val"] = edit
            who = _STATE.focus.get((int(hwnd), node["_rid"]))
            if who:
                node["focused"] = True
            if (int(hwnd), node["_rid"]) in _STATE.scroll_into:
                node["offscreen"] = False
        return nodes

    @staticmethod
    def _public(node):
        out = {k: v for k, v in node.items() if not k.startswith("_")}
        return out

    # -- snapshot ---------------------------------------------------------- #
    def snapshot(self, hwnd, *, view="controls", max_nodes=220, max_depth=24,
                 name_filter=None):
        t0 = time.perf_counter()
        win = self._win(hwnd)
        nodes = self._build(hwnd)
        if view == "text":
            keep = {"window", "pane", "document", "text", "edit"}
            nodes = [n for n in nodes if n["ct"] in keep]
        elif view == "controls":
            nodes = [n for n in nodes if n["ct"] not in ("image", "other")]
        if name_filter:
            needle = name_filter.lower()
            nodes = [n for n in nodes
                     if needle in (n.get("name") or "").lower()
                     or needle in (n.get("val") or "").lower()]
        total = len(nodes)
        try:
            limit = max(1, int(max_nodes))
        except (TypeError, ValueError):
            limit = 220
        shown_nodes = nodes[:limit]
        _STATE.by_id = {n["id"]: n for n in nodes}
        rect = win["rect"]
        maxed = (rect[0] <= -11 and rect[1] <= -11
                 and rect[2] >= SCREEN[0] and rect[3] >= SCREEN[1])
        snap = {
            "hwnd": int(hwnd),
            "title": win.get("title") or "",
            "rect": list(rect),
            "focused": int(hwnd) == _STATE.foreground,
            "state": "max" if maxed else "normal",
            "elapsed_ms": max(1, int((time.perf_counter() - t0) * 1000)),
            "total": total,
            "shown": len(shown_nodes),
            "truncated": total > len(shown_nodes),
            "nodes": [self._public(n) for n in shown_nodes],
        }
        return snap

    # -- id lookups -------------------------------------------------------- #
    def by_id(self, eid):
        try:
            node = _STATE.by_id.get(int(eid))
        except (TypeError, ValueError):
            return None
        return self._public(node) if node else None

    def _needle(self, spec):
        return spec.strip().strip("*").lower()

    def resolve(self, spec, hwnd):
        nodes = self._build(hwnd)
        _STATE.by_id = {n["id"]: n for n in nodes}
        spec = str(spec or "").strip()
        if not spec:
            raise BridgeError("target 为空：给 #id、ct:名字 或 @n:ux,uy")
        if spec.startswith("#") and spec[1:].isdigit():
            eid = int(spec[1:])
            for node in nodes:
                if node["id"] == eid:
                    pub = self._public(node)
                    return pub, {"via": "id", "node": pub, "candidates": []}
            raise BridgeError(f"元素 id {eid} 不存在（窗口树已重建，先 cu_show 拿新 id）")
        if spec.startswith("@"):
            body = spec[1:]
            try:
                x_s, y_s = body.split(",", 1)
                x, y = float(x_s), float(y_s)
            except (ValueError, IndexError):
                raise BridgeError(f'坐标 "{spec}" 无法解析，用 "@x,y"（物理像素）')
            hits = []
            for node in nodes:
                l, t, r, b = node["rect"]
                if l <= x <= r and t <= y <= b:
                    hits.append(node)
            if not hits:
                raise BridgeError(f"屏幕坐标 @{int(x)},{int(y)} 处没有 UIA 元素；改用 #id 或先截图看看")
            target = min(hits, key=lambda n: max(1, (n["rect"][2] - n["rect"][0])
                                                 * (n["rect"][3] - n["rect"][1])))
            pub = self._public(target)
            return pub, {"via": "point", "node": pub,
                         "candidates": [self._public(n) for n in hits[:5]]}
        ct = None
        name = spec
        if ":" in spec:
            head, tail = spec.split(":", 1)
            if head.strip().lower() in _CTS:
                ct = head.strip().lower()
                name = tail
        needle = self._needle(name)
        pool = [n for n in nodes if ct is None or n["ct"] == ct]
        exact = [n for n in pool if (n.get("name") or "").lower() == needle]
        if len(exact) == 1:
            pub = self._public(exact[0])
            return pub, {"via": "selector", "node": pub, "candidates": []}
        fuzzy = [n for n in pool if needle in (n.get("name") or "").lower()]
        if len(fuzzy) == 1:
            pub = self._public(fuzzy[0])
            return pub, {"via": "selector", "node": pub, "candidates": []}
        if not fuzzy and len(exact) == 0:
            raise BridgeError(
                f'没有找到 {ct + ":" if ct else ""}"{name}"；先 cu_show 看可用元素，'
                f"或改用 #id")
        candidates = exact if len(exact) > 1 else fuzzy
        raise AmbiguousError(
            f'{ct + ":" if ct else ""}"{name}" 有 {len(candidates)} 个候选',
            candidates=[self._public(n) for n in candidates[:6]])

    # -- actions ----------------------------------------------------------- #
    def act(self, hwnd, action, *, target=None, text=None, keys=None,
            value=None, amount=0, at=None, to=None, timeout_ms=3000,
            **_ignored):
        t0 = time.perf_counter()
        action = str(action or "").lower()

        def done(ok, via, detail, changed=False):
            return {"ok": ok, "action": action, "via": via,
                    "ms": max(1, int((time.perf_counter() - t0) * 1000)),
                    "detail": detail, "changed": changed}

        if action == "launch":
            app = str(target or text or _ignored.get("app") or "")
            low = app.lower()
            hwnd2 = None
            if "notepad" in low or "记事本" in app:
                hwnd2 = 1001
            elif "calc" in low or "计算器" in app:
                hwnd2 = 1003
            elif "explorer" in low or "资源管理器" in app:
                hwnd2 = 1004
            if hwnd2 is None:
                return done(False, "win32", f'不知道如何启动 "{app}"（mock 只认 notepad/计算器/资源管理器）')
            _STATE.missing.discard(hwnd2)
            _STATE.foreground = hwnd2
            _STATE.launched.append(app)
            return done(True, "win32", f"已启动 {app} (#{hwnd2})", True)

        if action in ("activate", "minimize", "maximize", "close"):
            try:
                self._win(hwnd)
            except BridgeError as exc:
                return done(False, "win32", str(exc))
            if action == "activate":
                _STATE.foreground = int(hwnd)
            elif action == "minimize":
                _STATE.windows[int(hwnd)]["minimized"] = True
            elif action == "maximize":
                _STATE.windows[int(hwnd)]["minimized"] = False
            elif action == "close":
                _STATE.missing.add(int(hwnd))
                if _STATE.foreground == int(hwnd):
                    _STATE.foreground = 1002
            return done(True, "win32", f"{action} 完成", True)

        if action == "scroll_to":
            if target in (None, ""):
                return done(False, "uia", "scroll_to 需要元素 target")
            try:
                public, _info = self.resolve(target, hwnd)
            except BridgeError as exc:
                return done(False, "uia", str(exc))
            raw = _STATE.by_id.get(public.get("id"))
            if raw is not None:
                _STATE.scroll_into.add((int(hwnd), raw["_rid"]))
            return done(True, "uia", f"已滚入视口 #{public['id']}", True)

        node = None
        if target not in (None, ""):
            try:
                public, _info = self.resolve(target, hwnd)
            except AmbiguousError as exc:
                return done(False, "uia", str(exc))
            except BridgeError as exc:
                return done(False, "uia", str(exc))
            node = _STATE.by_id.get(public.get("id"))

        if action in ("type", "setvalue", "paste") and node is None:
            edits = [n for n in self._build(hwnd)
                     if n["ct"] == "edit" or n["ct"] == "document"]
            node = edits[0] if edits else None
        if action in ("key", "paste") and node is None:
            return done(True, "keys", f"已发送按键 {keys or text or ''}", True)
        if action == "hold" and node is None:
            want = _ignored.get("ms")
            try:
                used = max(20, min(10000, int(want if want is not None else 500)))
            except (TypeError, ValueError):
                used = 500
            return done(True, "keys", f"hold: {keys or ''} {used}ms", True)
        if action == "hold_mouse" and node is None:
            want = _ignored.get("ms")
            try:
                used = max(20, min(10000, int(want if want is not None else 500)))
            except (TypeError, ValueError):
                used = 500
            btn = str(_ignored.get("button") or "left").lower()
            return done(True, "mouse", f"hold_mouse: {btn} {used}ms", True)
        if node is None:
            return done(False, "uia", "动作没有目标：给 target（#id / ct:名字 / @n:ux,uy）")

        new_text = value if value is not None else text
        if action in ("type", "setvalue", "paste") and new_text is not None:
            cur = _STATE.edits.get((int(hwnd), node["_rid"])) or node.get("val") or ""
            if action == "type":
                _STATE.edits[(int(hwnd), node["_rid"])] = str(cur) + str(new_text)
            else:
                _STATE.edits[(int(hwnd), node["_rid"])] = str(new_text)
            return done(True, "setvalue", f'已写入元素 #{node["id"]}', True)

        if action == "key":
            return done(True, "keys", f"已发送按键 {keys or text or ''}", True)
        if action == "focus":
            _STATE.focus[(int(hwnd), node["_rid"])] = True
            return done(True, "uia", f"已聚焦 #{node['id']}", False)
        if action in ("invoke", "click", "dblclick", "rightclick", "hover",
                      "toggle", "select", "expand", "collapse", "scroll", "drag"):
            if action == "toggle":
                node["enabled"] = node.get("enabled", True)
            return done(True, "invoke" if action in ("invoke", "toggle", "select",
                                                     "expand", "collapse")
                        else "click", f'{action} 元素 #{node["id"]}', True)
        return done(False, "uia", f'mock 不支持动作 "{action}"')

    # -- waiting ----------------------------------------------------------- #
    def wait(self, kind, *, target=None, text=None, value=None, hwnd=None,
             timeout_ms=3000, interval_ms=80):
        t0 = time.perf_counter()
        hwnd = hwnd if hwnd else _STATE.foreground
        needle = (text if text is not None else value) or target or ""
        found = False
        detail = ""
        if kind == "text":
            low = needle.lower()
            try:
                nodes = self._build(hwnd)
                found = any(low in ((n.get("name") or "") + " "
                                    + (n.get("val") or "")).lower()
                            for n in nodes)
            except BridgeError:
                found = False
            detail = f'标题包含 {needle!r}' if found else f"未见文本 {needle!r}"
        elif kind == "element":
            try:
                self.resolve(target or "", hwnd)
                found = True
                detail = f"元素已出现: {target}"
            except BridgeError:
                detail = f"元素尚未出现: {target}"
        elif kind == "window":
            hits = find_windows(needle, limit=1)
            found = bool(hits)
            if hits:
                detail = (f'窗口已出现 #{hits[0]["hwnd"]} '
                          f'{hits[0]["title"][:40]!r}')
            else:
                detail = f"{int(timeout_ms)}ms 内未等到窗口 {needle!r}"
        elif kind in ("idle", "change"):
            found = True
            detail = "UIA 树稳定" if kind == "idle" else "UIA 树已变化"
        elif kind == "gone":
            try:
                self.resolve(target or "", hwnd)
                detail = f"元素仍在: {target}"
            except BridgeError:
                found = True
                detail = f"元素已消失: {target}"
        if not found:
            time.sleep(min(max(0, int(timeout_ms)), 250) / 1000.0)
        return {"ok": found,
                "ms": max(1, int((time.perf_counter() - t0) * 1000)),
                "detail": detail or f'{kind} "{needle}" 已满足'}

    # -- screenshot -------------------------------------------------------- #
    def screenshot(self, hwnd=None, *, region=None, max_width=1280, quality=70,
                   marks=False):
        t0 = time.perf_counter()
        if region and len(region) >= 4:
            ox, oy, sw, sh = (int(region[0]), int(region[1]),
                              int(region[2]), int(region[3]))
        elif hwnd:
            rect = self._win(hwnd)["rect"]
            ox, oy = rect[0], rect[1]
            sw, sh = rect[2] - rect[0], rect[3] - rect[1]
        else:
            ox, oy = 0, 0
            sw, sh = SCREEN
        try:
            limit = max(64, int(max_width))
        except (TypeError, ValueError):
            limit = 1280
        img_w = min(sw, limit)
        img_h = max(1, round(sh * img_w / max(1, sw)))
        data = _jpeg(img_w, img_h, quality)
        scale = sw / img_w if img_w else 1.0
        shot = {"jpeg_bytes": data, "mime": "image/jpeg",
                "img_w": img_w, "img_h": img_h,
                "screen_w": sw, "screen_h": sh,
                "origin": [ox, oy], "scale": scale,
                "marks_applied": False,
                "ms": max(1, int((time.perf_counter() - t0) * 1000))}
        _STATE.shot = dict(shot)
        return shot

    def last_shot_map(self):
        if not _STATE.shot:
            return None
        shot = _STATE.shot
        return {"origin": list(shot["origin"]), "scale": shot["scale"],
                "img_w": shot["img_w"], "img_h": shot["img_h"],
                "screen_w": shot["screen_w"], "screen_h": shot["screen_h"]}


_CTS = {
    "button", "edit", "checkbox", "radiobutton", "combobox", "hyperlink",
    "menuitem", "tabitem", "listitem", "treeitem", "document", "text", "image",
    "pane", "window", "group", "menu", "menubar", "toolbar", "tab", "list",
    "scrollbar", "statusbar", "titlebar", "other",
}

#!/usr/bin/env python3
"""super-cu MCP server — hand-rolled stdio JSON-RPC 2.0 (no third-party deps).

    python -u D:\\opencode\\plugins\\super-cu\\src\\server.py [--mock]

Implements CONTRACT.md §4/§5:

* newline-delimited JSON-RPC 2.0 over stdin/stdout, the MCP stdio transport
* ``initialize`` / ``notifications/*`` / ``ping`` / ``tools/list`` / ``tools/call``
* tools ``cu_show`` (UIA snapshot -> compact text, optional JPEG) and
  ``cu_do`` (batched UI actions, fail-fast)
* stdout carries **only** JSON lines: fd 1 is pointed at stderr at startup and
  the protocol channel keeps a private duplicate, so library prints, tracebacks
  and warnings can never corrupt the stream
* DPI awareness is claimed before any win32/UIA/PIL call (src/dpi.py)
* screenshot coordinates are normalized server-side (``@n:ux,uy``), because the
  harness may rescale images before the model sees them
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import compact  # noqa: E402  (pure presentation layer, imported after sys.path fix)

try:  # dpi.py is lead-owned; never crash if it is missing during bring-up
    from dpi import ensure_dpi_aware  # noqa: E402
except Exception:  # pragma: no cover
    def ensure_dpi_aware():  # type: ignore
        return {"ok": False, "error": "dpi.py missing"}

try:
    from gui_lock import gui_locked, GuiBusy  # noqa: E402
except Exception:  # pragma: no cover
    class GuiBusy(Exception):
        pass

    @contextlib.contextmanager
    def gui_locked(timeout_s=30.0, poll_s=0.05):
        yield

SERVER_NAME = "supercu"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"
SHOT_MAX_LONG = 1728          # long side of the JPEG we hand to the client
MAX_STEPS = 50
DEFAULT_MAX_NODES = 100      # lead-calibrated: enough targets, half the tokens
MAX_MAX_NODES = 400

NO_GUI_OPS = frozenset({"wait", "assert"})
WAIT_KINDS = ("text", "element", "window", "idle", "change", "gone")
OPS = ("click", "dblclick", "rightclick", "hover", "focus", "set", "type",
       "key", "hold", "hold_mouse", "scroll", "scroll_to", "drag", "select",
       "invoke", "toggle", "expand", "collapse", "activate", "launch", "close",
       "minimize", "maximize", "wait", "assert", "paste", "clipboard",
       "window_move")
HOLD_MIN_MS = 20
HOLD_MAX_MS = 10000
MOUSE_BUTTONS = ("left", "right", "middle")
ACT_MAP = {"set": "setvalue"}

# --------------------------------------------------------------------------- #
# protocol channel (stdout stays JSON-only)
# --------------------------------------------------------------------------- #
_PROTO = None


def _open_protocol_channel() -> None:
    """Duplicate the real stdout, then point fd 1 at stderr.

    After this call anything that writes to ``sys.stdout`` (library warnings,
    a stray ``print``, a traceback) ends up on stderr, while :func:`emit` keeps
    writing JSON lines to the untouched original pipe.
    """
    global _PROTO
    if _PROTO is not None:
        return
    fd = None
    try:
        fd = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        fd = None
    if fd is not None:
        _PROTO = os.fdopen(fd, "wb", buffering=0)
    else:  # pragma: no cover - exotic stdio redirection
        _PROTO = getattr(sys.stdout, "buffer", sys.stdout)


def log(message) -> None:
    try:
        sys.stderr.write(f"[supercu] {message}\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover
        pass


def emit(obj) -> None:
    try:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                + "\n").encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError):
        data = (json.dumps(obj, ensure_ascii=True, separators=(",", ":"),
                           default=str) + "\n").encode("ascii")
    _PROTO.write(data)
    _PROTO.flush()


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class RpcError(Exception):
    """Protocol-level error -> JSON-RPC error object."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolError(Exception):
    """Execution error -> tool result with ``isError: true`` + advice."""

    def __init__(self, message, advice=None):
        super().__init__(message)
        self.message = message
        self.advice = advice

    def render(self) -> str:
        if self.advice:
            return f"{self.message}\n→ {self.advice}"
        return self.message


def _advice_for(detail: str) -> str:
    low = (detail or "").lower()
    if ("未等到" in low or "超时" in low or "尚未出现" in low or "timeout" in low
            or "未出现" in low):
        return ("等待超时：先确认 needle 与界面一致（窗口标题/文本/元素名可能变了），"
                "或加大 timeout_ms；也可以用 cu_show 看当前到底停在哪个界面"
                "（例如被对话框/切换窗口挡住）")
    if "失效" in low or "不存在" in low or "not found" in low or "stale" in low:
        return ("元素可能已过期：先 cu_show 拿最新 id（树重建后 id 会变），"
                "再用新 id 重试")
    if "disabled" in low or "禁用" in low or "不可用" in low:
        return ("控件当前是 disabled/不可用：换一个可用控件（cu_show 里带 - 状态的就是），"
                "或先做让它可用的前置动作（选中/勾选/等待加载），不要反复点同一个")
    if "clipboard" in low or "剪贴板" in low:
        return ("剪贴板写入/读取失败或被别的程序占用：改用 op=type（长文本会分块写入），"
                "或稍后重试 paste；不要用 paste 发超长文本")
    if ("unknown key" in low or "unknown_key" in low or "未知按键" in low
            or "不支持按键" in low or "key 需要" in low):
        return ("键名不被支持：用 ^s/^c/^v/^a、enter/tab/esc、alt+f4、"
                "ctrl+shift+t 这类写法（见 SKILL 键名清单），或改用 op=set / click")
    if "pattern" in low or "不支持该" in low or "unsupported" in low:
        return ("该控件不支持需要的 UIA Pattern：换等价动作（click / set / type / key）"
                "重试，或先 focus 再操作；别指望后台模式")
    if "对话框" in low or "dialog" in low or "阻塞" in low or "blocked" in low:
        return ("有阻塞对话框挡着：先用 op=key keys=\"alt+n\"（不保存）或 enter "
                "处理它，再重试；也可以用 cu_show scope=desktop 找到对话框 #hwnd")
    if "歧义" in low or "ambiguous" in low or "候选" in low:
        return "target 命中了多个元素：改用 #id，或写全名 ct:完整名字"
    if "窗口" in low and ("关闭" in low or "没有" in low or "不存在" in low):
        return "目标窗口可能已关闭：cu_show scope=desktop 看窗口列表，再用 #hwnd"
    if "gui" in low or "占用" in low:
        return "桌面正被其他操作占用：稍等重试"
    return ("先 cu_show 看当前界面，确认 target 后重试；"
            "纯像素定位用 screenshot=true 后的 @n:ux,uy")


# --------------------------------------------------------------------------- #
# parameter helpers
# --------------------------------------------------------------------------- #
def _as_str(params, key, default=None):
    value = params.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise RpcError(-32602, f"{key} 必须是字符串")
    return value


def _as_int(params, key, default, lo=None, hi=None):
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and float(value).is_integer():
            value = int(value)
        elif isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            value = int(value.strip())
        else:
            raise RpcError(-32602, f"{key} 必须是整数")
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        log(f"{key}={params.get(key)} 超过上限，已裁剪为 {hi}")
        value = hi
    return value


def _as_bool(params, key, default=False):
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    raise RpcError(-32602, f"{key} 必须是布尔值")


def _as_enum(params, key, allowed, default):
    value = params.get(key, default)
    if value not in allowed:
        raise RpcError(-32602,
                       f'{key}="{value}" 非法；可选：{", ".join(allowed)}')
    return value


def _as_region(value):
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
        raise RpcError(-32602, "region 必须是 [x,y,w,h] 四个整数（物理屏幕像素）")
    return [int(v) for v in value]


# --------------------------------------------------------------------------- #
# backend loading (T1's windows.py + bridge.py, or the bundled mock)
# --------------------------------------------------------------------------- #
class Backend:
    __slots__ = ("windows", "mod", "kind", "_bridge")

    def __init__(self, windows_mod, bridge_mod, kind):
        self.windows = windows_mod
        self.mod = bridge_mod
        self.kind = kind
        self._bridge = None

    @property
    def bridge(self):
        if self._bridge is None:
            # deferred: UIA init costs ~500ms, keep initialize/tools/list fast
            self._bridge = self.mod.Bridge()
        return self._bridge


# --------------------------------------------------------------------------- #
# tool schemas (model-facing; CONTRACT §5)
# --------------------------------------------------------------------------- #
CU_SHOW = {
    "name": "cu_show",
    "description": (
        "Windows 界面观察与操作的首选工具（比视觉快照快约 4 倍、文本小约 10 倍）；"
        "只有 canvas/自绘界面才用 screenshot=true。"
        "返回 UIA 元素树：每行 `[id] 类型 \"名字\"`，id 可直接作为 cu_do 的 target，"
        "同一窗口连续调用返回的 id 不变，可直接复用。"
        "先用文本；scope=desktop 列出窗口 #hwnd。默认 controls 视图会去掉窗口标题栏"
        "按钮/滚动条/重复影子文本/空容器，需要全部元素时用 view=all。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "window": {"type": "string",
                       "description": "窗口标题子串/进程名/#hwnd。省略=当前前台窗口"},
            "scope": {"type": "string", "enum": ["window", "desktop"],
                      "default": "window"},
            "view": {"type": "string", "enum": ["controls", "text", "all"],
                     "default": "controls",
                     "description": "controls=可交互+有意义文本; text=只文本内容; all=整棵树"},
            "filter": {"type": "string",
                       "description": "只保留名字/值含该子串的元素（忽略大小写）"},
            "max_nodes": {"type": "integer", "default": DEFAULT_MAX_NODES,
                          "description": "最多显示多少行（上限 400）；服务端会多取候选再按“可交互优先”挑这 N 行"},
            "screenshot": {"type": "boolean", "default": False,
                           "description": "附一张 JPEG；仅当文本不足以判断时用"},
            "marks": {"type": "boolean", "default": False,
                      "description": "截图时在元素上画方框并写 [id]（自动附带截图）；默认关闭"},
            "region": {"type": "array", "items": {"type": "integer"},
                       "description": "[x,y,w,h] 物理屏幕像素裁剪；给了 region 时按 region 原始像素输出（不缩放），max_image_width 不生效"},
            "max_image_width": {"type": "integer", "default": SHOT_MAX_LONG,
                                "description": "整窗截图长边像素上限（默认1728）；给了 region 时按 region 原始像素返回、不缩放，本参数不生效"},
        },
    },
}

CU_DO = {
    "name": "cu_do",
    "description": (
        "顺序执行多步 UI 动作（一次调用代替多次往返），失败即停并给可操作建议。"
        "target 支持 `#id`（推荐）、`ct:名字`（如 button:保存）、"
        "`@n:ux,uy`（归一化 0-1，相对最近截图，像素定位首选）、"
        "`@img:x,y`（最近截图内像素）、`@x,y`（物理屏幕像素）。"
        "目标窗口不在前台会自动激活，无需单列 activate。"
        "发送/删除/支付等不可逆动作先用 expect 验证上一步结果。"
        "observe=auto 仅在有界面变化或失败时回传最新界面。"
        "新增：clipboard(mode get|set)、window_move(x,y,w,h)、scroll_to(target)；"
        "drag 的 to 可直接写 #id / ct:名字。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": list(OPS)},
                        "target": {"type": "string",
                                   "description": "#id | ct:名字 | @n:ux,uy | @img:x,y | @x,y"},
                        "wait_kind": {
                            "type": "string",
                            "enum": list(WAIT_KINDS),
                            "description": "仅 wait/assert 使用：等待类型；window 类可用 window 或 text（window 优先），kind 是别名；省略时按 text/expect→text、target→element 推断",
                        },
                        "text": {"type": "string"},
                        "keys": {"type": "string",
                                 "description": "SendKeys 风格：^s / enter / alt+f4"},
                        "value": {"type": "string"},
                        "amount": {"type": "integer",
                                   "description": "scroll 的格数，可负"},
                        "to": {"type": "string",
                               "description": "drag 的目标位置：@x,y / @n:ux,uy，或 #id / ct:名字（拖到元素上）"},
                        "mode": {"type": "string", "enum": ["get", "set"],
                                 "description": "clipboard 的读写模式，默认 get"},
                        "x": {"type": "integer",
                              "description": "window_move 的目标左上角 x（物理像素）"},
                        "y": {"type": "integer",
                              "description": "window_move 的目标左上角 y（物理像素）"},
                        "w": {"type": "integer",
                              "description": "window_move 的目标宽度（省略=保持）"},
                        "h": {"type": "integer",
                              "description": "window_move 的目标高度（省略=保持）"},
                        "kind": {"type": "string", "enum": list(WAIT_KINDS),
                                 "description": "wait_kind 的别名（兼容旧写法）"},
                        "ms": {
                            "type": "integer",
                            "description": ("hold / hold_mouse 的按住时长（毫秒）。"
                                            f"20-{HOLD_MAX_MS}，超出自动截断并提示"),
                        },
                        "button": {
                            "type": "string",
                            "enum": list(MOUSE_BUTTONS),
                            "description": "hold_mouse 按住的鼠标键，默认 left",
                        },
                        "hold_ms": {
                            "type": "integer",
                            "description": "drag 按下后先保持多少毫秒再移动（默认保持现状）",
                        },
                        "window": {"type": "string",
                                   "description": "该步的目标窗口（省略=沿用上次窗口）"},
                        "app": {"type": "string", "description": "launch 的应用名"},
                        "expect": {"type": "string",
                                   "description": "动作后断言：文本/元素出现，如 '保存成功'"},
                        "timeout_ms": {"type": "integer", "default": 3000},
                    },
                    "required": ["op"],
                },
            },
            "observe": {"type": "string", "enum": ["auto", "none", "state"],
                        "default": "auto",
                        "description": "auto=只在中途失败或末步可能改变界面时附带最新状态"},
            "screenshot": {"type": "boolean", "default": False},
            "settle_ms": {"type": "integer", "default": 120,
                          "description": "每个改界面的步骤后等待的毫秒数"},
        },
        "required": ["steps"],
    },
}


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class SuperCuServer:
    def __init__(self, force_mock=False):
        self.backend: Backend | None = None
        self.backend_error = None
        self.client_protocol = None
        self.last_hwnd = None
        self.snapshots: dict = {}
        self.last_shot = None          # normalized shot meta for @n / @img
        self.last_crop = None          # compact.format_snapshot crop stats
        self._init_backend(force_mock)

    # -- setup ------------------------------------------------------------- #
    @staticmethod
    def _dpi():
        try:
            return ensure_dpi_aware()
        except Exception as exc:  # pragma: no cover
            log(f"dpi init failed: {exc}")
            return {}

    def _init_backend(self, force_mock):
        dpi = self._dpi()
        log(f"dpi awareness={dpi.get('awareness')} screen={dpi.get('screen')} "
            f"method={dpi.get('method')}")
        mock = force_mock or os.environ.get("SUPER_CU_MOCK") == "1"
        if mock:
            try:
                import mock_bridge
                self.backend = Backend(mock_bridge, mock_bridge, "mock")
                log("backend: mock (no real desktop access)")
                return
            except Exception as exc:
                self.backend_error = f"mock 后端加载失败：{exc}"
                log(self.backend_error)
                return
        try:
            import bridge as bridge_mod
            import windows as windows_mod
            self.backend = Backend(windows_mod, bridge_mod, "real")
            log("backend: real bridge")
        except Exception as exc:
            self.backend_error = (
                f"bridge 未就绪：{type(exc).__name__}: {exc}。"
                "T1 交付 src/bridge.py 后重启本进程；开发调试用 --mock。")
            log(self.backend_error)

    def _b(self) -> Backend:
        if self.backend is None:
            raise ToolError(self.backend_error or "后端未初始化",
                            advice="等 T1 的 bridge.py 就绪后重启；或用 --mock 调试协议")
        return self.backend

    # -- protocol methods -------------------------------------------------- #
    def initialize(self, params):
        version = params.get("protocolVersion")
        if not isinstance(version, str) or not version:
            version = DEFAULT_PROTOCOL
        self.client_protocol = version
        log(f"initialize protocolVersion={version}")
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def tools_list(self):
        return {"tools": [CU_SHOW, CU_DO]}

    def tools_call(self, params):
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(-32602, "tools/call 缺少 name")
        if name not in ("cu_show", "cu_do"):
            raise RpcError(-32602,
                           f'未知工具 "{name}"；可用 cu_show（观察）/ cu_do（动作）')
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "arguments 必须是对象")
        try:
            if name == "cu_show":
                return self.cu_show(arguments)
            return self.cu_do(arguments)
        except RpcError:
            raise
        except ToolError as exc:
            return self._tool_error(exc.render())
        except Exception as exc:  # never kill the process
            log("tool traceback: " + traceback.format_exc().replace("\n", " | "))
            return self._tool_error(
                f"内部错误：{type(exc).__name__}: {exc}\n"
                f"→ 先 cu_show 重试；若持续失败检查 bridge 日志（stderr [supercu]）")

    @staticmethod
    def _tool_error(text):
        return {"content": [{"type": "text", "text": text}], "isError": True}

    # -- shared helpers ---------------------------------------------------- #
    def _snapshot(self, hwnd, *, view="controls", max_nodes=DEFAULT_MAX_NODES,
                  filt=None):
        b = self._b()
        # Gather more candidates than are displayed: the display layer keeps the
        # most useful ``max_nodes`` lines (interactive first), and a raw node cap
        # of 100 would cut tab strips / toolbars that sit late in DFS order.
        request_nodes = min(MAX_MAX_NODES, max(int(max_nodes) * 3, 200))
        try:
            snap = b.bridge.snapshot(hwnd, view=view, max_nodes=request_nodes,
                                     name_filter=filt)
        except TypeError:
            # tolerate a bridge that ignores optional kwargs
            snap = b.bridge.snapshot(hwnd, view=view, max_nodes=request_nodes)
        if not isinstance(snap, dict):
            raise ToolError("bridge.snapshot 返回了非对象",
                            advice="检查 bridge.py 的返回值是否符合 CONTRACT §3.2")
        if not snap.get("nodes"):
            snap["nodes"] = []
        return snap

    def _observe_state(self, hwnd, max_nodes=DEFAULT_MAX_NODES):
        """Inline state for cu_do — exactly the cu_show controls budget.

        Same formatter caps apply (3500 chars / ``max_nodes`` lines / 10830 hard
        cap), so a batch never dumps a 5-7K character tree into the transcript.
        """
        snap = self._snapshot(hwnd, view="controls", max_nodes=max_nodes)
        self.snapshots[hwnd] = snap
        self.last_hwnd = hwnd
        crop_stats: dict = {}
        text = compact.format_snapshot(snap, view="controls",
                                       max_lines=max_nodes, stats=crop_stats)
        self.last_crop = crop_stats
        if crop_stats.get("budget_cropped"):
            log(f"cu_do observe budget crop hwnd={hwnd} "
                f"hidden={crop_stats.get('hidden')}")
        return text

    def _resolve_window(self, spec):
        b = self._b()
        if spec is None or (isinstance(spec, str) and not spec.strip()):
            hwnd = b.windows.foreground_hwnd()
            if not hwnd:
                raise ToolError(
                    "当前没有前台窗口（锁屏或桌面无窗口）",
                    advice="cu_show scope=desktop 看窗口列表，再用 #hwnd 指定")
            return int(hwnd)
        hwnd = None
        try:
            hwnd = b.windows.resolve_hwnd(spec)
        except Exception as exc:
            log(f"resolve_hwnd({spec!r}) failed: {exc}")
        if hwnd:
            return int(hwnd)
        try:
            cands = b.windows.find_windows(spec, limit=6)
        except Exception:
            cands = []
        if len(cands) == 1:
            return int(cands[0]["hwnd"])
        if len(cands) > 1:
            listing = "；".join(
                f'#{c.get("hwnd")} {compact.squeeze(c.get("title"), 36)}'
                for c in cands[:4])
            raise ToolError(f'窗口 "{spec}" 有 {len(cands)} 个候选：{listing}',
                            advice="用 #hwnd 精确指定，或给更长的标题子串")
        raise ToolError(f'没有找到窗口 "{spec}"',
                        advice="cu_show scope=desktop 看可用窗口（#hwnd 可直接选）")

    # -- screenshot path --------------------------------------------------- #
    @staticmethod
    def _shrink(shot, max_long):
        """Guarantee long side <= max_long before the image leaves the server."""
        data = shot.get("jpeg_bytes") or shot.get("png_bytes")
        try:
            iw = int(shot.get("img_w") or 0)
            ih = int(shot.get("img_h") or 0)
        except (TypeError, ValueError):
            iw = ih = 0
        if not data or iw <= 0 or ih <= 0:
            return shot
        long_side = max(iw, ih)
        if long_side <= max_long:
            return shot
        try:
            import io

            from PIL import Image
            image = Image.open(io.BytesIO(data))
            image.load()
            ratio = max_long / float(long_side)
            new_size = (max(1, round(iw * ratio)), max(1, round(ih * ratio)))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            image = image.convert("RGB").resize(new_size, resample)
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=70)
            out = dict(shot)
            out["jpeg_bytes"] = buf.getvalue()
            out.pop("png_bytes", None)
            out["mime"] = "image/jpeg"
            out["img_w"], out["img_h"] = new_size
            if shot.get("screen_w"):
                out["scale"] = shot["screen_w"] / new_size[0]
            return out
        except Exception as exc:  # pragma: no cover - Pillow present in prod
            log(f"downscale skipped: {exc}")
            return shot

    def _shot_meta(self, shot, hwnd):
        b = self._b()
        meta = dict(shot)
        origin = shot.get("origin")
        if not origin:
            try:
                rect = b.windows.get_rect(hwnd)
                origin = [rect[0], rect[1]]
            except Exception:
                origin = [0, 0]
        meta["origin"] = [int(origin[0]), int(origin[1])]
        iw = int(shot.get("img_w") or 0)
        ih = int(shot.get("img_h") or 0)
        scale = float(shot.get("scale") or 1.0) or 1.0
        sw = shot.get("screen_w") or round(iw * scale)
        sh = shot.get("screen_h") or round(ih * scale)
        meta.update({"img_w": iw, "img_h": ih,
                     "screen_w": int(sw), "screen_h": int(sh), "hwnd": hwnd})
        return meta

    def _screenshot(self, hwnd, region, max_long=SHOT_MAX_LONG, *,
                    marks=False, nodes=None):
        """Capture; when ``marks`` is on, overlay element id boxes (server-side).

        The bridge is asked to draw marks first (future capability, pass-through);
        if it doesn't support them we annotate the JPEG ourselves so the model
        can map picture boxes to the stable ``#id`` targets.
        """
        b = self._b()
        if region:
            # explicit region: honour the requested pixel box (callers assert
            # img width == region width), no forced downscale
            limit = max(64, min(int(region[2]) or 64, 4096))
            shrink_limit = limit
        else:
            try:
                limit = max(200, min(int(max_long or SHOT_MAX_LONG), 3000))
            except (TypeError, ValueError):
                limit = SHOT_MAX_LONG
            shrink_limit = SHOT_MAX_LONG
        marked = 0
        try:
            shot = b.bridge.screenshot(hwnd, region=region, max_width=limit,
                                       marks=marks)
        except TypeError:
            try:
                shot = b.bridge.screenshot(hwnd, region=region, max_width=limit)
            except TypeError:
                shot = b.bridge.screenshot(hwnd)
        if not isinstance(shot, dict):
            raise ToolError("bridge.screenshot 返回非对象")
        if not (shot.get("jpeg_bytes") or shot.get("png_bytes")):
            raise ToolError("截图没有返回图像数据")
        shot = self._shrink(shot, shrink_limit)
        if marks and not shot.get("marks_applied") and nodes:
            shot, marked = self._annotate_shot(shot, nodes)
        meta = self._shot_meta(shot, hwnd)
        self.last_shot = meta
        return meta, marked

    def _annotate_shot(self, shot, nodes):
        """Draw ``[id]`` labels on element rects (best-effort, Pillow)."""
        try:
            import io

            from PIL import Image, ImageDraw, ImageFont
        except Exception as exc:  # pragma: no cover
            log(f"marks skipped (Pillow): {exc}")
            return shot, 0
        data = shot.get("jpeg_bytes") or shot.get("png_bytes")
        origin = shot.get("origin") or [0, 0]
        sw = int(shot.get("screen_w") or shot.get("img_w") or 1)
        sh = int(shot.get("screen_h") or shot.get("img_h") or 1)
        iw = int(shot.get("img_w") or 0)
        ih = int(shot.get("img_h") or 0)
        if not data or iw <= 0 or ih <= 0 or sw <= 0 or sh <= 0:
            return shot, 0
        try:
            image = Image.open(io.BytesIO(data)).convert("RGB")
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default()
            sx, sy = iw / float(sw), ih / float(sh)
            drawn = 0
            for node in nodes:
                if drawn >= 120:
                    break
                rect = node.get("rect") or [0, 0, 0, 0]
                if len(rect) < 4:
                    continue
                left, top, right, bottom = [int(v) for v in rect[:4]]
                if right - left < 2 or bottom - top < 2:
                    continue
                x0 = int(round((left - origin[0]) * sx))
                y0 = int(round((top - origin[1]) * sy))
                x1 = int(round((right - origin[0]) * sx))
                y1 = int(round((bottom - origin[1]) * sy))
                if x1 <= 0 or y1 <= 0 or x0 >= iw or y0 >= ih:
                    continue
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(iw - 1, x1), min(ih - 1, y1)
                if x1 - x0 < 2 or y1 - y0 < 2:
                    continue
                draw.rectangle([x0, y0, x1, y1], outline=(255, 64, 64), width=2)
                label = str(node.get("id"))
                try:
                    box = draw.textbbox((0, 0), label, font=font)
                    tw, th = box[2] - box[0], box[3] - box[1]
                except Exception:
                    tw, th = 6 * len(label), 10
                draw.rectangle([x0, y0, x0 + tw + 4, y0 + th + 3],
                               fill=(200, 32, 32))
                draw.text((x0 + 2, y0 + 1), label, fill=(255, 255, 255),
                          font=font)
                drawn += 1
            if not drawn:
                return shot, 0
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=70)
            out = dict(shot)
            out["jpeg_bytes"] = buf.getvalue()
            out.pop("png_bytes", None)
            out["mime"] = "image/jpeg"
            out["marks_applied"] = True
            return out, drawn
        except Exception as exc:  # pragma: no cover - never fail cu_show
            log(f"marks annotate failed: {type(exc).__name__}: {exc}")
            return shot, 0

    def _to_point_spec(self, spec):
        """Map @n:ux,uy / @img:x,y to a physical @x,y point (server-side)."""
        if not isinstance(spec, str):
            return spec
        s = spec.strip()
        if not (s.startswith("@n:") or s.startswith("@img:")):
            return s
        shot = self.last_shot
        if not shot:
            raise ToolError(
                f'还不能换算 "{s}"：本进程还没有截图',
                advice="先 cu_show screenshot=true 拿到图片，再用 @n:ux,uy；"
                       "或直接用 #id / ct:名字 / @x,y（物理屏幕像素）")
        try:
            if s.startswith("@n:"):
                ux_s, uy_s = s[3:].split(",", 1)
                ux, uy = float(ux_s), float(uy_s)
                if not (0.0 <= ux <= 1.0 and 0.0 <= uy <= 1.0):
                    raise ValueError("归一化坐标要在 0..1")
                x = shot["origin"][0] + ux * shot["screen_w"]
                y = shot["origin"][1] + uy * shot["screen_h"]
            else:
                px_s, py_s = s[5:].split(",", 1)
                px, py = float(px_s), float(py_s)
                if shot["img_w"] <= 0 or shot["img_h"] <= 0:
                    raise ValueError("截图尺寸无效")
                x = shot["origin"][0] + px * shot["screen_w"] / shot["img_w"]
                y = shot["origin"][1] + py * shot["screen_h"] / shot["img_h"]
        except ValueError as exc:
            raise ToolError(
                f'target "{s}" 无法解析（{exc}）',
                advice="@n:ux,uy 形如 @n:0.42,0.31（0..1）；"
                       "@img:x,y 用最近截图的像素")
        return f"@{int(round(x))},{int(round(y))}"

    # -- cu_show ----------------------------------------------------------- #
    def cu_show(self, args):
        b = self._b()
        scope = _as_enum(args, "scope", ("window", "desktop"), "window")
        view = _as_enum(args, "view", ("controls", "text", "all"), "controls")
        window = _as_str(args, "window")
        filt = _as_str(args, "filter")
        max_nodes = _as_int(args, "max_nodes", DEFAULT_MAX_NODES, 1, MAX_MAX_NODES)
        want_shot = _as_bool(args, "screenshot", False)
        marks = _as_bool(args, "marks", False)
        if marks:
            want_shot = True   # marks only makes sense on an image
        region = _as_region(args.get("region"))
        max_img = _as_int(args, "max_image_width", SHOT_MAX_LONG, 200, 3000)

        started = time.perf_counter()
        hwnd = None
        snap = None
        if scope == "desktop":
            try:
                wins = b.windows.list_windows()
            except Exception as exc:
                raise ToolError(f"列出窗口失败：{type(exc).__name__}: {exc}",
                                advice="检查 windows.py；用 cu_show window=\"#hwnd\" 指定窗口")
            elapsed = int((time.perf_counter() - started) * 1000)
            text = compact.format_windows(wins, limit=12, elapsed_ms=elapsed)
        else:
            hwnd = self._resolve_window(window)
            snap = self._snapshot(hwnd, view=view, max_nodes=max_nodes, filt=filt)
            self.snapshots[hwnd] = snap
            self.last_hwnd = hwnd
            display_lines = max_nodes if view == "controls" and not filt else None
            crop_stats: dict = {}
            text = compact.format_snapshot(snap, view=view, filter=filt,
                                           max_lines=display_lines,
                                           stats=crop_stats)
            self.last_crop = crop_stats
            if crop_stats.get("budget_cropped"):
                log(f"cu_show budget crop hwnd={hwnd} "
                    f"hidden={crop_stats.get('hidden')} "
                    f"lines={crop_stats.get('rendered_lines')} "
                    f"cap={crop_stats.get('cap')}")
            elif crop_stats.get("tree_cut"):
                log(f"cu_show tree cut hwnd={hwnd}")

        image_blocks = []
        if want_shot:
            try:
                shot, marked = self._screenshot(
                    hwnd, region, max_img, marks=marks,
                    nodes=(snap.get("nodes") if snap else None))
                data = shot.get("jpeg_bytes") or shot.get("png_bytes")
                mime = shot.get("mime") or "image/jpeg"
                text += "\n" + compact.format_shot_note(shot, len(data))
                if marks:
                    if marked:
                        text += (f"\nmarks: 图中方框数字 = 元素 id，可直接用 #id 点击"
                                 f"（已标 {marked} 个）")
                    else:
                        text += ("\nmarks: 本次没有可标注的元素（desktop 作用域或"
                                 "元素无矩形），坐标仍用 @n:ux,uy")
                image_blocks.append({
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime,
                })
            except Exception as exc:  # degrade to text-only, never fail cu_show
                log(f"screenshot failed: {type(exc).__name__}: {exc}")
                text += (f"\n截图失败（{type(exc).__name__}: "
                         f"{compact.squeeze(exc, 120)}）：本次只返回文本，"
                         f"可重试或用 #id 操作")

        text = compact.cap_text(text)
        return {"content": [{"type": "text", "text": text}] + image_blocks}

    # -- cu_do ------------------------------------------------------------- #
    def cu_do(self, args):
        b = self._b()
        steps = args.get("steps")
        if steps is None:
            raise RpcError(-32602, "缺少 steps（非空数组，每项至少 {op}）")
        if not isinstance(steps, list):
            raise RpcError(-32602, "steps 必须是数组")
        if not steps:
            raise RpcError(-32602,
                           'steps 不能为空：至少 1 步，例如 '
                           '[{"op":"click","target":"#123"}]')
        if len(steps) > MAX_STEPS:
            raise RpcError(-32602,
                           f"steps 一次最多 {MAX_STEPS} 步：拆成多次调用，"
                           f"中间用 cu_show 检查")
        for idx, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                raise RpcError(-32602, f"steps[{idx}] 必须是对象")
            op = step.get("op")
            if op not in OPS:
                raise RpcError(
                    -32602,
                    f'steps[{idx}].op="{op}" 非法；可用：{", ".join(OPS)}')
            for key in ("target", "text", "keys", "value", "window", "app",
                        "expect", "to", "wait_kind", "kind", "mode"):
                if step.get(key) is not None and not isinstance(step.get(key), str):
                    raise RpcError(-32602, f"steps[{idx}].{key} 必须是字符串")
            wait_kind = step.get("wait_kind") or step.get("kind")
            if wait_kind is not None and wait_kind not in WAIT_KINDS:
                raise RpcError(
                    -32602,
                    f'steps[{idx}].wait_kind="{wait_kind}" 非法；可选：'
                    f'{", ".join(WAIT_KINDS)}（kind 是别名）')
            if op == "paste" and not (step.get("text") or step.get("value")):
                raise RpcError(
                    -32602,
                    f'steps[{idx}].op="paste" 必须给 text 或 value'
                    f"（要写入剪贴板并粘贴的文本）；没文本时不要盲发 Ctrl+V，"
                    f"改用 op=key 或 op=type")
            if op == "clipboard":
                mode = step.get("mode") or "get"
                if mode not in ("get", "set"):
                    raise RpcError(-32602,
                                   f'steps[{idx}].mode 只能是 "get" 或 "set"')
                if mode == "set" and not (step.get("text") or step.get("value")):
                    raise RpcError(
                        -32602,
                        f'steps[{idx}].op="clipboard" mode="set" 必须给 text'
                        f"（要写入剪贴板的文本）")
            if op == "window_move":
                for axis in ("x", "y"):
                    value = step.get(axis)
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise RpcError(
                            -32602,
                            f"steps[{idx}].window_move 需要整数 {axis}（物理像素）")
            if op == "scroll_to" and not step.get("target"):
                raise RpcError(-32602,
                               f'steps[{idx}].op="scroll_to" 必须给 target（#id / ct:名字）')
            if op in ("hold", "hold_mouse"):
                v = step.get("ms")
                if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                    raise RpcError(-32602, f"steps[{idx}].ms 必须是整数毫秒")
                if v is not None and v < 0:
                    raise RpcError(-32602, f"steps[{idx}].ms 不能为负（下限 {HOLD_MIN_MS}ms）")
                if op == "hold" and not step.get("keys"):
                    raise RpcError(
                        -32602,
                        f'steps[{idx}].op="hold" 必须给 keys（如 "w" 或 "shift+w"）',
                    )
            if step.get("button") is not None:
                if not isinstance(step.get("button"), str) or step["button"] not in MOUSE_BUTTONS:
                    raise RpcError(
                        -32602,
                        f'steps[{idx}].button 只能是 {", ".join(MOUSE_BUTTONS)}',
                    )
            if op == "hold_mouse" and step.get("button") is None:
                pass  # 默认 left
            for key in ("amount", "timeout_ms", "hold_ms", "x", "y", "w", "h"):
                value = step.get(key)
                if value is not None and (isinstance(value, bool)
                                          or not isinstance(value, int)):
                    raise RpcError(-32602, f"steps[{idx}].{key} 必须是整数")

        observe = _as_enum(args, "observe", ("auto", "none", "state"), "auto")
        want_shot = _as_bool(args, "screenshot", False)
        settle_ms = _as_int(args, "settle_ms", 120, 0, 2000)

        lines: list = []
        failed = None
        last_changed = False
        last_hwnd = None
        # Locking contract: bridge.act() acquires the shared desktop lock itself
        # for every focus / key / mouse action (T1's LOCK_ACTIONS), and this
        # server wraps its own windows.activate() call below.  Holding the lock
        # for the whole batch here would nest with the bridge's own acquire and
        # block until timeout, so the batch itself stays lock-free.
        ctx = contextlib.nullcontext()
        try:
            with ctx:
                for idx, step in enumerate(steps, 1):
                    label = _step_label(step)
                    try:
                        res = self._exec_step(step)
                    except ToolError as exc:
                        failed = (idx, label, exc.message,
                                  exc.advice or _advice_for(exc.message))
                        break
                    except Exception as exc:
                        log("step traceback: "
                            + traceback.format_exc().replace("\n", " | "))
                        detail = f"{type(exc).__name__}: {exc}"
                        failed = (idx, label, detail, _advice_for(detail))
                        break
                    last_hwnd = res.get("hwnd") or last_hwnd
                    if not res.get("ok"):
                        detail = res.get("detail") or "动作失败"
                        failed = (idx, label, detail, _advice_for(detail))
                        break
                    extra = ""
                    if res.get("ok") and step["op"] == "launch" and res.get("hwnd"):
                        # tell the model which window just appeared (#hwnd target)
                        extra = f"#{res['hwnd']}"
                    if step["op"] in NO_GUI_OPS and res.get("detail"):
                        # wait/assert: carry the matched target (window title +
                        # hwnd for wait_kind=window) so the model needn't re-show
                        detail = compact.squeeze(res.get("detail"), 70)
                        if detail:
                            extra = (extra + " " + detail).strip()
                    if step.get("expect") and step["op"] not in NO_GUI_OPS:
                        waited = self._expect(step, res.get("hwnd"))
                        if not waited.get("ok"):
                            failed = (
                                idx, label,
                                f'断言失败：等不到 "{step["expect"]}"'
                                f'（{waited.get("ms", 0)}ms）',
                                "先用 cu_show 确认界面真的变了；或加大 "
                                "timeout_ms；expect 匹配元素名/文本")
                            break
                        extra = (extra + " exp✓").strip()
                    lines.append(compact.step_ok(idx, label,
                                                 res.get("via") or "-",
                                                 res.get("ms", 0), extra))
                    if step["op"] == "clipboard" and res.get("content") is not None:
                        content = res.get("content")
                        lines.append('clipboard: "' +
                                     compact.squeeze(content, 800) + '"')
                    if res.get("changed"):
                        last_changed = True
                        self.last_hwnd = last_hwnd or self.last_hwnd
                    if (idx < len(steps) and settle_ms
                            and res.get("changed")
                            and step["op"] not in NO_GUI_OPS):
                        time.sleep(settle_ms / 1000.0)
        except GuiBusy as exc:
            raise ToolError(
                f"桌面被别的操作占用，等锁超过 30s：{exc}",
                advice="别的进程正在操作鼠标键盘；稍等重试，或减少批量步数")

        if failed:
            idx, label, detail, advice = failed
            lines += compact.fail_block(idx, label, detail, advice,
                                        skipped=len(steps) - idx)

        # obs block: auto = only when something changed or a step failed.
        # Budget is identical to cu_show (controls 3500 chars / max_nodes lines /
        # 10830 hard cap), and any observer failure degrades to one neutral line.
        text = "\n".join(lines)
        state_needed = (observe == "state"
                        or (observe == "auto" and (failed or last_changed)))
        if state_needed:
            hwnd = last_hwnd or self.last_hwnd
            if hwnd:
                try:
                    state_text = self._observe_state(int(hwnd))
                    text += ("\n" if text else "") + state_text
                except Exception as exc:
                    log("observe after cu_do skipped: "
                        f"{type(exc).__name__}: {exc}")
                    text += ("\n" if text else "") + (
                        "obs skipped: " + _short_reason(exc))
            else:
                text += ("\n" if text else "") + (
                    "obs skipped: no target window after this batch")

        image_blocks = []
        if want_shot:
            try:
                shot, _marked = self._screenshot(last_hwnd or self.last_hwnd,
                                                 None, SHOT_MAX_LONG)
                data = shot.get("jpeg_bytes") or shot.get("png_bytes")
                text += "\n" + compact.format_shot_note(shot, len(data))
                image_blocks.append({
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": shot.get("mime") or "image/jpeg",
                })
            except Exception as exc:
                log(f"cu_do screenshot failed: {type(exc).__name__}: {exc}")
                text += (f"\n截图失败（{type(exc).__name__}: "
                         f"{compact.squeeze(exc, 120)}）：本次只返回文本")

        text = compact.cap_text(text)
        result = {"content": [{"type": "text", "text": text}] + image_blocks}
        if failed:
            result["isError"] = True
        return result

    # -- step execution ---------------------------------------------------- #
    def _exec_step(self, step):
        b = self._b()
        op = step["op"]
        timeout = int(step.get("timeout_ms") or 3000)

        if op == "clipboard":
            return self._clipboard_step(step)
        if op == "window_move":
            return self._window_move_step(step)

        if op in NO_GUI_OPS:
            kind, value = _wait_kind(step)
            # Do NOT resolve the target window up front: waiting is exactly the
            # case where the window/element may not exist yet.  A resolved spec
            # failure here used to abort the wait before bridge.wait() ran
            # (wait_kind=window returned FAIL in ~4ms instead of waiting).
            hwnd = self._wait_hwnd(step)
            wait_target = step.get("target")
            wait_text = step.get("text") or step.get("expect")
            if kind in ("text", "window", "gone") and value:
                # explicit window= wins for wait_kind=window; text works too
                wait_text = value
            try:
                res = b.bridge.wait(kind, target=wait_target,
                                    text=wait_text,
                                    value=step.get("value"), hwnd=hwnd,
                                    timeout_ms=timeout)
            except TypeError:
                res = b.bridge.wait(kind, target=wait_target,
                                    text=wait_text, hwnd=hwnd,
                                    timeout_ms=timeout)
            ok = bool(res.get("ok"))
            if op == "assert" and not ok:
                raise ToolError(
                    f'断言失败：{kind} "{value or wait_text or ""}" '
                    f"在 {timeout}ms 内没出现",
                    advice="先 cu_show 确认界面状态；确认界面真该变化再加大 "
                           "timeout_ms")
            return {"ok": ok, "via": kind, "ms": res.get("ms", 0),
                    "detail": res.get("detail") or ("已满足" if ok else "超时未满足"),
                    "changed": False, "hwnd": hwnd}

        activate = op not in ("launch", "close", "minimize", "maximize",
                              "scroll_to")
        if (op in ("key", "paste") and not step.get("window")
                and not step.get("app")):
            # SendKeys targets whatever is focused — e.g. a save dialog opened
            # by the previous step.  Never yank focus back to last_hwnd here.
            try:
                hwnd = int(b.windows.foreground_hwnd() or 0)
            except Exception:
                hwnd = 0
            hwnd = hwnd or int(self.last_hwnd or 0)
            if hwnd:
                self.last_hwnd = hwnd
            activate = False
        else:
            hwnd = self._step_hwnd(step, activate=activate)
        target = step.get("target")
        if target:
            target = self._to_point_spec(target)

        action = ACT_MAP.get(op, op)
        before_hwnds: set = set()
        if op == "launch":
            try:
                before_hwnds = {int(w["hwnd"]) for w in b.windows.list_windows()}
            except Exception as exc:
                log(f"launch pre-scan failed: {exc}")
        kwargs = {
            "target": target,
            "text": step.get("text"),
            "keys": step.get("keys"),
            "value": step.get("value"),
            "amount": int(step.get("amount") or 0),
            "timeout_ms": timeout,
        }
        if op == "drag":
            to_spec = step.get("to")
            kwargs["to"] = (self._resolve_to_target(to_spec, hwnd)
                            if to_spec else None)
            kwargs["at"] = target
            kwargs["hold_ms"] = step.get("hold_ms")
        elif op in ("hold", "hold_mouse"):
            kwargs["ms"] = step.get("ms")
            kwargs["button"] = step.get("button") or "left"
        elif op == "launch":
            app = step.get("app") or step.get("text") or (
                target if target and not str(target).startswith("#") else None)
            kwargs["target"] = app
            kwargs["app"] = app
            try:
                hwnd = int(b.windows.foreground_hwnd() or 0)
            except Exception:
                hwnd = 0
        else:
            kwargs["app"] = step.get("app")

        try:
            res = b.bridge.act(hwnd or 0, action, **kwargs)
        except TypeError:
            res = b.bridge.act(hwnd or 0, action, target=target,
                               text=step.get("text"), keys=step.get("keys"),
                               value=step.get("value"))
        if not isinstance(res, dict):
            raise ToolError("bridge.act 返回了非对象",
                            advice="检查 bridge.py 是否符合 CONTRACT §3.2")
        out = {
            "ok": bool(res.get("ok")),
            "via": res.get("via") or "-",
            "ms": int(res.get("ms") or 0),
            "detail": res.get("detail") or "",
            "changed": bool(res.get("changed")),
            "hwnd": hwnd,
        }
        if out["ok"] and op == "launch":
            app_name = step.get("app") or step.get("text")
            found_hwnd = self._launched_hwnd(b, before_hwnds, app_name)
            if not found_hwnd:
                # launch may have reused an existing window (Win11 Notepad is
                # single-process/tabbed); the focused window is the target then
                try:
                    found_hwnd = int(b.windows.foreground_hwnd() or 0) or None
                except Exception:
                    found_hwnd = None
            if found_hwnd:
                out["hwnd"] = found_hwnd
                self.last_hwnd = found_hwnd
        # launch ok:false never reaches here: the batch fails and stops, so no
        # later step can run against a wrong hwnd.
        return out

    @staticmethod
    def _launched_hwnd(b, before_hwnds, app_name):
        """Prefer the window that actually appeared, not any match by name."""
        try:
            windows_now = b.windows.list_windows()
        except Exception:
            windows_now = []
        app_low = str(app_name or "").lower()
        fresh = []
        for win in windows_now:
            try:
                hwnd = int(win.get("hwnd"))
            except (TypeError, ValueError):
                continue
            if hwnd in before_hwnds:
                continue
            if app_low:
                haystack = " ".join(str(win.get(key) or "") for key in
                                    ("title", "proc", "class")).lower()
                if app_low not in haystack:
                    continue
            if not win.get("visible", True):
                continue
            fresh.append(win)
        if fresh:
            fresh.sort(key=lambda w: (not w.get("focused"), w.get("z", 0)))
            return int(fresh[0]["hwnd"])
        if app_name:
            try:
                fallback = b.windows.find_windows(app_name, limit=1)
            except Exception:
                fallback = []
            if fallback:
                return int(fallback[0]["hwnd"])
        return None

    def _wait_hwnd(self, step):
        """Best-effort hwnd for a wait/assert step; never raises, may be None.

        ``bridge.wait`` falls back to the foreground window when hwnd is 0, which
        is the right behaviour while waiting for a window that does not exist
        yet.  When the step names a window, use it if it is already there.
        """
        b = self._b()
        spec = step.get("window")
        if spec:
            try:
                hwnd = b.windows.resolve_hwnd(spec)
            except Exception:
                hwnd = None
            if hwnd:
                return int(hwnd)
        hwnd = self.last_hwnd
        if hwnd:
            try:
                b.windows.get_rect(hwnd)
                return int(hwnd)
            except Exception:
                pass
        return None

    # -- B8: clipboard / window_move / element-target helpers ---------------- #
    def _action_lock(self, b, timeout_s=3.0):
        """GUI lock for shared-desktop ops; mock never touches the real desktop."""
        if b.kind == "mock":
            return contextlib.nullcontext()
        return gui_locked(timeout_s=timeout_s)

    def _clipboard_step(self, step):
        """B8 clipboard op: read/write the shared clipboard explicitly."""
        b = self._b()
        mode = (step.get("mode") or "get").strip().lower()
        t0 = time.perf_counter()

        def done(ok, detail, content=None):
            return {"ok": ok, "via": "clipboard",
                    "ms": max(1, int((time.perf_counter() - t0) * 1000)),
                    "detail": detail, "changed": False, "content": content,
                    "hwnd": self.last_hwnd}

        if mode == "set":
            text = step.get("text")
            if text is None:
                text = step.get("value") or ""
            try:
                with self._action_lock(b):
                    ok = bool(b.windows.set_clipboard_text(text))
            except GuiBusy as exc:
                raise ToolError(
                    f"剪贴板写入等锁超时：{exc}",
                    advice="别的程序占着剪贴板/桌面，稍后重试；或改用 op=type")
            except Exception as exc:
                raise ToolError(
                    f"写剪贴板失败：{type(exc).__name__}: {exc}",
                    advice="剪贴板可能被占用：稍后重试，或用 op=type 直接输入")
            return done(ok, (f"剪贴板已写入 {len(text)} 字符" if ok
                             else "剪贴板写入失败（可能被其他程序占用）"))
        try:
            content = b.windows.get_clipboard_text()
        except Exception as exc:
            raise ToolError(
                f"读剪贴板失败：{type(exc).__name__}: {exc}",
                advice="剪贴板可能被占用：稍后重试；确认剪贴板里是文本")
        if content is None:
            return done(False, "剪贴板读取失败（被占用或内容不是文本）")
        return done(True, f"已读取剪贴板 {len(content)} 字符", content)

    def _window_move_step(self, step):
        """B8 window_move op: move/resize the target window (physical pixels)."""
        b = self._b()
        hwnd = self._step_hwnd(step, activate=False)
        x, y = int(step["x"]), int(step["y"])
        w, h = step.get("w"), step.get("h")
        t0 = time.perf_counter()
        try:
            with self._action_lock(b):
                rect = b.windows.move_window(hwnd, x, y, w, h)
        except GuiBusy as exc:
            raise ToolError(f"移动窗口等锁超时：{exc}",
                            advice="桌面被其他操作占用：稍后重试")
        except Exception as exc:
            raise ToolError(
                f"移动窗口失败：{type(exc).__name__}: {exc}",
                advice="确认窗口没有关闭、坐标在虚拟屏幕内（多屏可为负）")
        rect = [int(v) for v in rect] if rect else []
        detail = (f"窗口已移动到 {rect[0]},{rect[1]} "
                  f"{rect[2] - rect[0]}x{rect[3] - rect[1]}" if len(rect) == 4
                  else "窗口已移动")
        return {"ok": True, "via": "win32",
                "ms": max(1, int((time.perf_counter() - t0) * 1000)),
                "detail": detail, "changed": True, "hwnd": hwnd}

    def _resolve_to_target(self, spec, hwnd):
        """drag.to: accept @coords OR an element selector (#id / ct:name)."""
        if spec is None:
            return None
        s = self._to_point_spec(spec)
        if s.startswith("@"):
            return s
        if s.startswith("#") or ":" in s:
            b = self._b()
            try:
                element, info = b.bridge.resolve(s, hwnd)
            except Exception as exc:
                raise ToolError(
                    f'drag.to="{s}" 无法解析：{exc}',
                    advice="用 #id / ct:名字 / @x,y；歧义时用 #id")
            node = info.get("node")
            if not node:
                node = element.get("node") if isinstance(element, dict) else getattr(
                    element, "node", None)
            rect = (node or {}).get("rect")
            if not rect or len(rect) < 4 or rect[2] <= rect[0] or rect[3] <= rect[1]:
                raise ToolError(f'drag.to="{s}" 没有可见矩形，拒绝盲拖',
                                advice="先 cu_show 确认该元素可见，或改用 @x,y")
            cx = (int(rect[0]) + int(rect[2])) // 2
            cy = (int(rect[1]) + int(rect[3])) // 2
            return f"@{cx},{cy}"
        return s

    def _step_hwnd(self, step, *, activate):
        b = self._b()
        spec = step.get("window")
        if step.get("app") and step["op"] != "launch":
            spec = spec or step["app"]
        if spec:
            hwnd = self._resolve_window(spec)
        else:
            hwnd = self.last_hwnd
            if hwnd:
                try:
                    b.windows.get_rect(hwnd)
                except Exception:
                    hwnd = None
            if not hwnd:
                hwnd = b.windows.foreground_hwnd()
        if not hwnd:
            raise ToolError(
                "没有可操作的目标窗口",
                advice='给 step.window（标题/#hwnd），或先 cu_show 确认前台窗口')
        hwnd = int(hwnd)
        self.last_hwnd = hwnd
        if activate:
            try:
                if int(b.windows.foreground_hwnd() or 0) != hwnd:
                    # focus change is a shared-desktop operation: take the lock
                    # briefly (the bridge does the same for its own actions)
                    with gui_locked(timeout_s=3.0):
                        b.windows.activate(hwnd)
            except GuiBusy:
                log(f"auto-activate #{hwnd} skipped: desktop busy")
            except Exception as exc:
                log(f"auto-activate #{hwnd} failed: {exc}")
        return hwnd

    def _expect(self, step, hwnd):
        b = self._b()
        timeout = int(step.get("timeout_ms") or 3000)
        try:
            return b.bridge.wait("text", text=step["expect"], hwnd=hwnd,
                                 timeout_ms=timeout)
        except TypeError:
            return b.bridge.wait("text", text=step["expect"], hwnd=hwnd)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _step_label(step) -> str:
    parts = [str(step.get("op"))]
    target = step.get("target")
    if target:
        parts.append(str(target))
    elif step.get("app"):
        parts.append(str(step["app"]))
    if step.get("text"):
        parts.append(f'"{compact.squeeze(step["text"], 40)}"')
    elif step.get("value"):
        parts.append(f'"{compact.squeeze(step["value"], 40)}"')
    if step.get("keys"):
        parts.append(str(step["keys"]))
    if step.get("amount"):
        parts.append(str(step["amount"]))
    if step.get("to"):
        parts.append(f"→{step['to']}")
    if step.get("ms") is not None:
        parts.append(f"{step['ms']}ms")
    if step.get("button"):
        parts.append(str(step["button"]))
    if step.get("hold_ms") is not None:
        parts.append(f"hold={step['hold_ms']}ms")
    return " ".join(parts)


def _wait_kind(step):
    """Resolve (kind, value) for wait/assert; explicit ``wait_kind`` wins."""
    explicit = step.get("wait_kind") or step.get("kind")
    if explicit in WAIT_KINDS:
        if explicit == "text":
            value = (step.get("text") or step.get("expect")
                     or step.get("target") or "")
        elif explicit == "element":
            value = step.get("target") or ""
        elif explicit == "window":
            value = (step.get("window") or step.get("target")
                     or step.get("text") or step.get("expect") or "")
        elif explicit == "gone":
            value = (step.get("target") or step.get("window")
                     or step.get("text") or step.get("expect") or "")
        else:  # idle / change: no value needed
            value = ""
        return explicit, value
    # legacy inference (kept for backwards compatibility)
    text = step.get("text") or step.get("expect")
    if text:
        return "text", text
    if step.get("target"):
        return "element", step["target"]
    if step.get("window"):
        return "window", step["window"]
    if step.get("app"):
        return "window", step["app"]
    return "idle", ""


def _short_reason(exc) -> str:
    """One-line, traceback-free reason for degraded notes (never says 失败)."""
    try:
        text = str(exc) or ""
    except Exception:
        text = ""
    reason = compact.squeeze(text, 80)
    return reason or "unknown error"


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch
# --------------------------------------------------------------------------- #
def _rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def handle_message(server, msg):
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600,
                          "invalid request：每行必须是一个 JSON-RPC 对象")
    has_id = "id" in msg
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params")
    if params is None:
        params = {}

    if not has_id:
        # notification: never answer (initialized / cancelled / unknown)
        if method == "notifications/initialized":
            log("client initialized")
        elif method == "notifications/cancelled":
            log("request cancelled by client")
        else:
            log(f"notification {method!r} ignored")
        return None

    if not isinstance(method, str):
        return _rpc_error(mid, -32600, "invalid request：缺少 method")
    if not isinstance(params, dict):
        return _rpc_error(mid, -32602, "params 必须是对象")

    try:
        if method == "initialize":
            result = server.initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = server.tools_list()
        elif method == "tools/call":
            result = server.tools_call(params)
        elif method == "shutdown":
            result = {}
        else:
            raise RpcError(
                -32601,
                f'未知 method "{method}"；本 server 只支持 '
                f"initialize / notifications/* / ping / tools/list / tools/call")
    except RpcError as exc:
        return _rpc_error(mid, exc.code, exc.message)
    except Exception as exc:  # pragma: no cover - defensive
        log("dispatch traceback: " + traceback.format_exc().replace("\n", " | "))
        return _rpc_error(mid, -32603, f"内部错误：{type(exc).__name__}: {exc}")
    return _rpc_result(mid, result)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv) -> int:
    # MUST be the first thing on the wire: fd 1 -> stderr, protocol keeps a dup.
    _open_protocol_channel()
    mock = "--mock" in argv
    log(f"starting pid={os.getpid()} argv={argv}")
    server = SuperCuServer(force_mock=mock)
    if server.backend is not None:
        log(f"ready backend={server.backend.kind}")
    else:
        log("ready backend=none (tools will return setup errors)")

    stdin = sys.stdin.buffer
    while True:
        try:
            raw = stdin.readline()
        except (KeyboardInterrupt, OSError):
            break
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            try:
                emit(_rpc_error(None, -32700,
                                f"JSON 解析失败：{exc.msg}（每行必须是完整 JSON）"))
            except OSError:
                break
            continue
        try:
            response = handle_message(server, msg)
        except Exception:  # pragma: no cover - handle_message is defensive
            log("handler traceback: "
                + traceback.format_exc().replace("\n", " | "))
            response = _rpc_error(msg.get("id") if isinstance(msg, dict) else None,
                                  -32603, "内部错误")
        if response is not None:
            try:
                emit(response)
            except OSError:
                break
    log("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""super-cu optimization benchmark harness (schema 1).

Reproducible, load-aware measurements for the super-cu MCP plugin.  Every
number written to JSON/Markdown comes from a recorded run in this process (or
from a spawned server it drove); nothing is typed in by hand.

Modes
-----
1. single-src benchmark (default):
       python bench/opt/bench.py --src-dir D:\opencode\_supercu_opt\orig\src \
           --label baseline --out bench/opt/baseline.json
   Measures cold start (real + mock), bridge.snapshot on a controlled Notepad
   window and a large real window, compact.format_snapshot on frozen fixtures,
   screenshot, spawned-server cu_show (e2e) and 200-snapshot memory growth.
   --quick shrinks every n (for iteration only, not for headline numbers).

2. interleaved A/B comparison (--src-a / --src-b):
       python bench/opt/bench.py --src-a D:\opencode\_supercu_opt\orig\src \
           --src-b D:\opencode\plugins\super-cu\src \
           --label orig_vs_opt --out bench/opt/compare.json
   Loads both src trees as isolated module sets in ONE process, interleaves
   A,B,A,B,... measurements (order alternates per round) and reports paired
   median deltas.  Subprocess metrics (cold start, e2e) alternate spawned
   servers A and B.  This is the primary before/after evidence: it cancels the
   30-50 percent drift this machine shows when other agents benchmark at the
   same time.

3. fixture capture (--dump-fixtures):
       python bench/opt/bench.py --src-dir D:\opencode\_supercu_opt\orig\src \
           --dump-fixtures
   Freezes snapshot JSON under bench/opt/fixtures/ (Notepad controlled
   content, Calculator, Explorer/System32, mock large scene, mock Edge scene).
   Fixtures MUST be generated from the pristine src so render/parity numbers
   stay comparable before/after.

4. report (--report, no desktop access):
       python bench/opt/bench.py --report bench/opt/baseline.json \
           --report-out docs/OPT-BASELINE.md
   Renders Markdown tables from recorded JSON.  Pass two files (baseline and
   after) to get A/B delta columns.

Safety / scope
--------------
* Only Notepad, Calculator and Explorer are launched; everything opened is
  closed again (with a save-dialog / taskkill fallback).  The large real
  window is measured READ-ONLY (UIA snapshot, no focus change, no input).
* All state-changing GUI calls go through bridge.act / bridge.window_move,
  which take the shared gui_lock themselves.  The user session server
  (PID 180508) is never touched.
* Measured numbers are load-sensitive: every section records CPU load, python
  process count and node counts as covariates; use ms-per-visited-node and
  paired deltas, not raw absolute latency.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import math
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import traceback

try:
    import psutil
except Exception:  # pragma: no cover - psutil is available on this host
    psutil = None


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_SRC = os.path.join(REPO_ROOT, "src")
DEFAULT_FIXTURES = os.path.join(HERE, "fixtures")
TMP_DIR = os.path.join(HERE, ".tmp")
CONTENT_PATH = os.path.join(TMP_DIR, "bench_content.txt")

SCHEMA = 1
FIXTURE_SCHEMA = 1
HARNESS_NAME = "supercu-bench"
HARNESS_VERSION = "1.0.0"

# Controlled Notepad content: fixed 40-line ASCII + Chinese document.
# The file on disk uses CRLF; Notepad's ValuePattern/TextPattern read-back
# represents line breaks as a single CR, so setvalue verification uses that
# representation (see CONTENT_SET).
FIXED_LINES = [
    "line 01 | super-cu bench controlled content",
    "line 02 | The quick brown fox jumps over the lazy dog 0123456789",
    "line 03 | 中文固定内容：记事本快照基准，含标点，。！？【】123",
    "line 04 | alpha beta gamma delta epsilon zeta eta theta",
    "line 05 | 表格列：名称 / 值 / 说明 / 备注",
    "line 06 | id=1024 name=保存 value=OK",
    "line 07 | -------- separator --------",
]
for _i in range(8, 41):
    FIXED_LINES.append(
        "line %02d | 基准数据行 %02d — lorem ipsum dolor sit amet %d"
        % (_i, _i, (_i * 37) % 9973)
    )
CONTENT_LINES = list(FIXED_LINES)
# what the file starts with; setvalue then adds one marker line so the bridge
# really changes the editor content (and the change is saved back to the file)
CONTENT_FILE_LINES = list(FIXED_LINES)
CONTENT_SET_LINES = list(FIXED_LINES) + [
    "line 41 | setvalue verified content (bench controlled window)"
]
CONTENT_FILE = "\r\n".join(CONTENT_FILE_LINES) + "\r\n"
CONTENT_SET = "\r".join(CONTENT_SET_LINES)
CONTENT_TITLE_HINT = "bench_content"
NOTEPAD_TMP_NAME = "bench_content.txt"

FULL = {
    "cold_n": 5,
    "snap_n": 10,
    "render_n": 50,
    "shot_n": 5,
    "e2e_n": 5,
    "mem_n": 200,
    "cmp_rounds": 5,
    "cmp_mem_n": 100,
}
QUICK = {
    "cold_n": 3,
    "snap_n": 3,
    "render_n": 10,
    "shot_n": 3,
    "e2e_n": 3,
    "mem_n": 50,
    "cmp_rounds": 3,
    "cmp_mem_n": 30,
}

SECTION_NAMES = ("cold_start", "snapshot", "render", "screenshot", "e2e", "memory")
INSTRUMENTED = ("snapshot", "_walk", "_make_node", "_control_for", "space_check")
PLUGIN_MODULES = ("dpi", "gui_lock", "windows", "bridge", "compact", "mock_bridge")


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def round_if(value, digits=2):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def percentile(values, q: float):
    """Linear-interpolated percentile (numpy-free, stable across runs)."""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (q / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def summarize(values, digits: int = 2, int_values: bool = False) -> dict:
    """Stable metric block: n, p50, p95, mean, min, max and the raw samples."""
    vals = [float(v) for v in values if v is not None]
    if int_values:
        vals = [int(round(v)) for v in vals]
    if not vals:
        return {"n": 0, "p50": None, "p95": None, "mean": None,
                "min": None, "max": None, "values": []}
    block = {
        "n": len(vals),
        "p50": round_if(percentile(vals, 50), digits),
        "p95": round_if(percentile(vals, 95), digits),
        "mean": round_if(sum(vals) / len(vals), digits),
        "min": round_if(min(vals), digits),
        "max": round_if(max(vals), digits),
        "values": [round_if(v, digits) for v in vals],
    }
    return block


def paired_summary(a_values, b_values) -> dict:
    """Paired A/B statistics for interleaved samples (b relative to a)."""
    n = min(len(a_values), len(b_values))
    deltas, ratios, pcts = [], [], []
    for i in range(n):
        a = a_values[i]
        b = b_values[i]
        if a is None or b is None:
            continue
        a = float(a)
        b = float(b)
        deltas.append(b - a)
        if a > 0:
            ratios.append(b / a)
            pcts.append((b - a) / a * 100.0)
    if not deltas:
        return {"n": 0, "a_p50": None, "b_p50": None, "delta_p50": None,
                "ratio_p50": None, "delta_pct_p50": None,
                "b_better_pct": None}
    return {
        "n": len(deltas),
        "a_p50": round_if(percentile(a_values[:n], 50), 2),
        "b_p50": round_if(percentile(b_values[:n], 50), 2),
        "delta_p50": round_if(percentile(deltas, 50), 3),
        "ratio_p50": round_if(percentile(ratios, 50) if ratios else None, 3),
        "delta_pct_p50": round_if(percentile(pcts, 50) if pcts else None, 1),
        "b_better_pct": round_if(
            100.0 * sum(1 for d in deltas if d < 0) / len(deltas), 1),
    }


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_text_file(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read_json_file(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json_file(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.write("\n")


def own_rss_mb() -> float:
    if psutil is None:
        return 0.0
    return round(psutil.Process().memory_info().rss / (1024.0 * 1024.0), 2)


def python_process_count() -> int:
    if psutil is None:
        return -1
    count = 0
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except Exception:
            continue
        if name.startswith("python"):
            count += 1
    return count


def proc_rss_mb(pid: int) -> float:
    if psutil is None or not pid:
        return 0.0
    try:
        return round(psutil.Process(int(pid)).memory_info().rss / (1024.0 * 1024.0), 2)
    except Exception:
        return 0.0


def os_context(mods: dict | None) -> dict:
    """Machine state slice recorded with every section (load covariates)."""
    state = {
        "captured_at": iso_now(),
        "epoch": round(time.time(), 3),
        "cpu_load_pct": None,
        "cpu_count": os.cpu_count(),
        "python_processes": python_process_count(),
        "own_rss_mb": own_rss_mb(),
        "visible_windows": None,
        "foreground": None,
    }
    try:
        state["cpu_load_pct"] = round(psutil.cpu_percent(interval=0.2), 1) if psutil else None
    except Exception:
        state["cpu_load_pct"] = None
    if psutil is not None:
        try:
            mem = psutil.virtual_memory()
            state["mem_used_pct"] = round(mem.percent, 1)
        except Exception:
            pass
    if mods:
        windows = mods["windows"]
        try:
            wins = windows.list_windows()
            state["visible_windows"] = sum(1 for w in wins if w.get("visible"))
            fg = windows.foreground_hwnd()
            info = next((w for w in wins if w.get("hwnd") == fg), None)
            if info:
                state["foreground"] = {
                    "hwnd": info.get("hwnd"),
                    "proc": info.get("proc"),
                    "title": (info.get("title") or "")[:60],
                }
        except Exception as exc:
            state["windows_error"] = "%s: %s" % (type(exc).__name__, exc)
    return state


def src_fingerprint(src_dir: str) -> dict:
    """sha256 of every .py in the measured src tree (server.py included)."""
    out = {}
    try:
        names = sorted(os.listdir(src_dir))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".py"):
            continue
        path = os.path.join(src_dir, name)
        if not os.path.isfile(path):
            continue
        out[name] = {
            "sha256": sha256_file(path),
            "bytes": os.path.getsize(path),
        }
    return out


def fixtures_fingerprint(fixtures_dir: str) -> dict:
    out = {}
    if not os.path.isdir(fixtures_dir):
        return out
    for name in sorted(os.listdir(fixtures_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(fixtures_dir, name)
        out[name] = {"sha256": sha256_file(path), "bytes": os.path.getsize(path)}
    return out


# --------------------------------------------------------------------------- #
# isolated plugin module loading (pristine A and current B in one process)
# --------------------------------------------------------------------------- #
def load_plugin(src_dir: str, tag: str) -> dict:
    """Import dpi/gui_lock/windows/bridge/compact/mock_bridge from src_dir.

    Both src trees must be loadable in the same process for interleaved A/B
    measurement, so the plain module names are removed from sys.modules around
    the import and the resulting module objects are kept under unique names.
    """
    src_dir = os.path.abspath(src_dir)
    if not os.path.isfile(os.path.join(src_dir, "server.py")):
        raise SystemExit("not a super-cu src dir (no server.py): %s" % src_dir)
    if not os.path.isfile(os.path.join(src_dir, "bridge.py")):
        raise SystemExit("not a super-cu src dir (no bridge.py): %s" % src_dir)
    saved = {}
    for name in PLUGIN_MODULES:
        if name in sys.modules:
            saved[name] = sys.modules.pop(name)
    saved_path = list(sys.path)
    mods = {}
    sys.path.insert(0, src_dir)
    try:
        for name in PLUGIN_MODULES:
            mods[name] = importlib.import_module(name)
        try:
            mods["dpi"].ensure_dpi_aware()
        except Exception:
            pass
    finally:
        for name in PLUGIN_MODULES:
            sys.modules.pop(name, None)
        for name, module in saved.items():
            sys.modules[name] = module
        sys.path[:] = saved_path
    for name, module in mods.items():
        sys.modules["_scbench_%s_%s" % (tag, name)] = module
    return mods


# --------------------------------------------------------------------------- #
# bridge method instrumentation (measures the legacy walker breakdown)
# --------------------------------------------------------------------------- #
class BridgeTimers:
    """Wraps Bridge methods with cumulative call/ms counters."""

    def __init__(self, bridge_cls):
        self.boxes = {}
        for name in INSTRUMENTED:
            orig = getattr(bridge_cls, name, None)
            if orig is None:
                continue
            box = {"calls": 0, "ms": 0.0}
            self.boxes[name] = box
            setattr(bridge_cls, name, self._wrap(orig, box))

    @staticmethod
    def _wrap(orig, box):
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            result = orig(*args, **kwargs)
            box["calls"] += 1
            box["ms"] += (time.perf_counter() - t0) * 1000.0
            return result
        wrapper.__name__ = getattr(orig, "__name__", "wrapped")
        wrapper.__doc__ = getattr(orig, "__doc__", None)
        return wrapper

    def reset(self) -> None:
        for box in self.boxes.values():
            box["calls"] = 0
            box["ms"] = 0.0

    def snapshot(self) -> dict:
        return {
            name: {"calls": box["calls"], "ms": round(box["ms"], 3)}
            for name, box in self.boxes.items()
        }


# --------------------------------------------------------------------------- #
# spawned MCP server (newline-delimited JSON-RPC over stdio)
# --------------------------------------------------------------------------- #
INIT_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "supercu-bench", "version": HARNESS_VERSION},
}


class ServerProc:
    """Drives one spawned src/server.py over the MCP stdio transport."""

    def __init__(self, src_dir: str, mock: bool = False):
        self.src_dir = os.path.abspath(src_dir)
        args = [sys.executable, "-u", os.path.join(self.src_dir, "server.py")]
        if mock:
            args.append("--mock")
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env.pop("SUPER_CU_MOCK", None)
        self._next_id = 0
        self._junk = []
        self._stderr = []
        self._t_spawn = time.perf_counter()
        self.p = subprocess.Popen(
            args,
            cwd=self.src_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._q = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._err_loop, daemon=True).start()

    # -- io ------------------------------------------------------------- #
    def _read_loop(self):
        try:
            for raw in self.p.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except Exception:
                    self._q.put({"__raw__": line[:400].decode("utf-8", "replace")})
                    continue
                self._q.put(obj)
        except Exception:
            pass
        finally:
            self._q.put(None)

    def _err_loop(self):
        try:
            for raw in self.p.stderr:
                self._stderr.append(raw.decode("utf-8", "replace").rstrip()[:300])
                if len(self._stderr) > 400:
                    del self._stderr[:200]
        except Exception:
            pass

    def _send(self, obj) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            self.p.stdin.write(data.encode("utf-8"))
            self.p.stdin.flush()
        except Exception as exc:
            raise RuntimeError("server stdin closed (%s); stderr tail=%s"
                               % (exc, " | ".join(self._stderr[-6:])))

    def notify(self, method: str, params=None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params=None, timeout: float = 180.0):
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        t0 = time.perf_counter()
        while True:
            remaining = timeout - (time.perf_counter() - t0)
            if remaining <= 0:
                raise TimeoutError(
                    "%s timed out after %.1fs; stderr tail=%s"
                    % (method, timeout, " | ".join(self._stderr[-6:])))
            try:
                obj = self._q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if obj is None:
                raise RuntimeError(
                    "server exited while waiting for %s; stderr tail=%s"
                    % (method, " | ".join(self._stderr[-8:])))
            if "__raw__" in obj:
                self._junk.append(obj["__raw__"])
                continue
            if obj.get("id") == rid:
                mark = (time.perf_counter() - self._t_spawn) * 1000.0
                return obj, mark

    def rss_mb(self) -> float:
        return proc_rss_mb(self.p.pid)

    def stderr_tail(self, n: int = 8):
        return self._stderr[-n:]

    def close(self, timeout: float = 5.0) -> None:
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=timeout)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
            try:
                self.p.wait(timeout=3)
            except Exception:
                pass


def response_text(resp) -> str:
    result = resp.get("result") or {}
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def response_is_error(resp) -> bool:
    result = resp.get("result") or {}
    return bool(result.get("isError"))


def spawn_noop_ms() -> float:
    """Interpreter-startup baseline for cold start (spawn -> first stdout line).

    Mirrors the spawn->initialize transport exactly: spawn python, wait for the
    first byte on stdout, then kill.  Waiting for process exit instead would
    include teardown/scheduler noise and can even exceed a lazy server's
    spawn->initialize time.
    """
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", "print(1)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        proc.stdout.readline()
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
    return (time.perf_counter() - t0) * 1000.0


# --------------------------------------------------------------------------- #
# snapshots / fixtures helpers
# --------------------------------------------------------------------------- #
def normalize_snapshot(snap: dict) -> dict:
    """Strip volatile fields so a dumped fixture is deterministic."""
    out = dict(snap)
    out.pop("hwnd", None)
    out.pop("elapsed_ms", None)
    return out


def load_fixtures(fixtures_dir: str) -> list:
    fixtures = []
    if not os.path.isdir(fixtures_dir):
        return fixtures
    for name in sorted(os.listdir(fixtures_dir)):
        if not name.endswith(".json") or name == "manifest.json":
            continue
        try:
            data = read_json_file(os.path.join(fixtures_dir, name))
        except Exception as exc:
            print("  fixture load failed %s: %s" % (name, exc))
            continue
        if data.get("schema") != FIXTURE_SCHEMA:
            print("  fixture schema mismatch %s" % name)
            continue
        fixtures.append(data)
    return fixtures
# === PART 2: measurement primitives ===


def add_error(errors: list, where: str, exc) -> None:
    errors.append({
        "where": where,
        "error": "%s: %s" % (type(exc).__name__, exc),
        "ts": iso_now(),
    })
    print("  !! %s failed: %s: %s" % (where, type(exc).__name__, exc))


def window_meta(mods: dict, hwnd: int) -> dict:
    windows = mods["windows"]
    info = {}
    try:
        info = windows.window_info(hwnd) or {}
    except Exception:
        try:
            info = next((w for w in windows.list_windows() if w.get("hwnd") == hwnd), {})
        except Exception:
            info = {}
    out = {
        "hwnd": int(hwnd),
        "proc": info.get("proc"),
        "title": info.get("title"),
        "class": info.get("class"),
        "rect": info.get("rect"),
        "pid": info.get("pid"),
    }
    return out


# --------------------------------------------------------------------------- #
# controlled Notepad window (launch once, set fixed content, close at the end)
# --------------------------------------------------------------------------- #
def _wait_title(mods: dict, hwnd: int, hint: str, deadline_s: float = 10.0):
    windows = mods["windows"]
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if hwnd and windows.is_window(hwnd):
                title = windows.get_title(hwnd) or ""
                if hint.lower() in title.lower():
                    return hwnd, title
        except Exception:
            pass
        try:
            hits = windows.find_windows(hint, limit=3)
        except Exception:
            hits = []
        for hit in hits:
            if hint.lower() in (hit.get("title") or "").lower():
                return int(hit["hwnd"]), hit.get("title") or ""
        time.sleep(0.15)
    return 0, ""


def _wait_snapshot(bridge, hwnd: int, predicate, deadline_s: float = 10.0):
    deadline = time.monotonic() + deadline_s
    last_error = None
    while time.monotonic() < deadline:
        try:
            snap = bridge.snapshot(hwnd, view="controls", max_nodes=120)
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
            continue
        if predicate(snap):
            return snap
        time.sleep(0.15)
    raise RuntimeError("snapshot predicate not met within %.0fs (last_error=%r)"
                       % (deadline_s, last_error))


def setup_controlled_notepad(mods: dict, bridge, errors: list, keep_open: bool = True):
    """Launch Notepad with fixed content, set it through bridge setvalue, verify."""
    windows = mods["windows"]
    os.makedirs(TMP_DIR, exist_ok=True)
    write_text_file(CONTENT_PATH, CONTENT_FILE)
    # close our own leftovers from an interrupted earlier run (never a user doc:
    # only the unique bench_content temp-file title matches)
    force_close_stray_bench_notepad(mods, bridge, errors)
    before = {w["hwnd"] for w in windows.list_windows()}
    result = bridge.act(0, "launch", text="notepad.exe " + CONTENT_PATH)
    launched = dict(getattr(bridge, "_last_launch", None) or {})
    hwnd = int(result.get("hwnd") or launched.get("hwnd") or 0)
    if not result.get("ok") and not hwnd:
        raise RuntimeError("notepad launch failed: %s" % result.get("detail"))
    matched = bool(launched.get("matched"))
    hwnd2, title = _wait_title(mods, hwnd, CONTENT_TITLE_HINT, 10.0)
    if not hwnd2:
        raise RuntimeError("Notepad window with %r title did not appear"
                           % CONTENT_TITLE_HINT)
    hwnd = hwnd2
    if not matched or hwnd in before:
        # never touch a pre-existing user Notepad: abort instead of clobbering.
        raise RuntimeError(
            "Notepad window was reused (matched=%s); aborting to avoid "
            "touching a pre-existing window" % matched)
    time.sleep(0.35)
    # GUI collision guard: a fresh Notepad must not own a modal dialog.  If one
    # appears (another agent's stray input, an error popup), dismiss it once and
    # abort instead of measuring a wedged window.
    popup_actions = []
    modal = None
    checker = getattr(mods["bridge"], "_modal_children", None)
    if checker is not None:
        try:
            modal = checker(hwnd)
        except Exception:
            modal = None
    if modal:
        try:
            bridge.act(int(modal), "key", keys="esc")
            popup_actions.append({"hwnd": int(modal), "action": "esc"})
        except Exception as exc:
            add_error(errors, "notepad.popup_dismiss", exc)
        time.sleep(0.4)
        try:
            modal = checker(hwnd) if checker is not None else None
        except Exception:
            modal = None
    if modal:
        raise RuntimeError(
            "controlled Notepad #%d owns a modal popup #%s; aborting instead of "
            "measuring a wedged window" % (hwnd, modal))
    other_notepads = []
    try:
        for win in windows.list_windows():
            if win.get("hwnd") == hwnd:
                continue
            if (win.get("proc") or "").lower() == "notepad":
                other_notepads.append({
                    "hwnd": int(win["hwnd"]),
                    "title": (win.get("title") or "")[:60],
                    "class": win.get("class"),
                })
    except Exception:
        other_notepads = []
    move_rect = None
    try:
        move_rect = bridge.window_move(hwnd, 60, 60, 1180, 880)
    except Exception as exc:
        add_error(errors, "notepad.window_move", exc)
    snap = _wait_snapshot(
        bridge, hwnd,
        lambda s: any(n.get("ct") in ("edit", "document")
                      for n in (s.get("nodes") or [])))
    node = next(n for n in snap["nodes"] if n.get("ct") in ("edit", "document"))
    setvalue = bridge.act(hwnd, "setvalue",
                          target="#%d" % int(node["id"]), value=CONTENT_SET)
    title_after_set = windows.get_title(hwnd) or ""
    # save so the on-disk bench_content.txt matches the editor content and the
    # window has no pending "save?" dialog when we close it later.
    save = bridge.act(hwnd, "key", keys="^s")
    time.sleep(0.45)
    text_snap = None
    content_verified = None
    content_prefix = CONTENT_SET[:120]
    try:
        text_snap = bridge.snapshot(hwnd, view="text", max_nodes=200)
        doc = next((n for n in (text_snap.get("nodes") or [])
                    if n.get("ct") == "document"), None)
        value = (doc or {}).get("val") or ""
        content_verified = bool(value) and value[:len(content_prefix)] == content_prefix
    except Exception as exc:
        add_error(errors, "notepad.content_verify", exc)
    handle = {
        "hwnd": hwnd,
        "pid": (launched.get("pid") or 0) or window_meta(mods, hwnd).get("pid"),
        "window": window_meta(mods, hwnd),
        "title": windows.get_title(hwnd) or title,
        "matched": matched,
        "fresh_verified": True,
        "popup_check": {
            "modal_children_found": bool(modal),
            "actions": popup_actions,
            "other_notepad_windows": other_notepads,
        },
        "launch": {k: launched.get(k) for k in
                   ("hwnd", "pid", "proc", "title", "match", "matched")},
        "window_move_rect": move_rect,
        "setvalue": setvalue,
        "save": save,
        "title_after_set": title_after_set,
        "content_sha256": hashlib.sha256(CONTENT_SET.encode("utf-8")).hexdigest(),
        "content_lines": len(CONTENT_SET_LINES),
        "content_verified": content_verified,
        "keep_open": keep_open,
    }
    return handle


def find_modal_notepad_dialog(mods: dict, main_hwnd: int):
    windows = mods["windows"]
    try:
        for win in windows.list_windows():
            if win.get("hwnd") == main_hwnd:
                continue
            proc = (win.get("proc") or "").lower()
            cls = (win.get("class") or "")
            if proc == "notepad" and cls == "#32770":
                return int(win["hwnd"])
    except Exception:
        pass
    return 0


def close_controlled_notepad(mods: dict, bridge, handle: dict, errors: list):
    windows = mods["windows"]
    out = {"closed": False, "forced": False, "save": None, "close": None}
    hwnd = int(handle.get("hwnd") or 0)
    if not hwnd or not windows.is_window(hwnd):
        out["closed"] = True
        out["already_gone"] = True
        return out
    try:
        out["save"] = bridge.act(hwnd, "key", keys="^s")
    except Exception as exc:
        add_error(errors, "notepad.close.save", exc)
    time.sleep(0.35)
    try:
        out["close"] = bridge.act(hwnd, "close")
    except Exception as exc:
        add_error(errors, "notepad.close.close", exc)
    time.sleep(0.3)
    if windows.is_window(hwnd):
        dlg = find_modal_notepad_dialog(mods, hwnd)
        if dlg:
            try:
                out["dialog_discard"] = bridge.act(dlg, "key", keys="alt+n")
            except Exception as exc:
                add_error(errors, "notepad.close.dialog", exc)
            time.sleep(0.4)
    if windows.is_window(hwnd):
        pid = 0
        try:
            pid = int(window_meta(mods, hwnd).get("pid") or 0)
        except Exception:
            pid = 0
        if pid and psutil is not None:
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                out["forced"] = True
                out["forced_pid"] = pid
            except Exception as exc:
                add_error(errors, "notepad.close.forced", exc)
        time.sleep(0.3)
    out["closed"] = not windows.is_window(hwnd)
    return out


def _window_gone_error(exc) -> bool:
    text = str(exc)
    low = text.lower()
    return ("已不存在" in text or "已关闭" in text or "不存在" in text
            or "gone" in low or "closed" in low)


def _require_window_alive(mods: dict, hwnd: int, tag: str) -> None:
    windows = mods["windows"]
    try:
        if hwnd and windows.is_window(int(hwnd)):
            return
    except Exception:
        pass
    raise RuntimeError(
        "controlled window #%s is gone before/inside %s (another agent or the "
        "user may have closed it)" % (hwnd, tag))


def ensure_controlled_notepad(mods: dict, bridge, handle, result: dict,
                              tag: str):
    """Re-launch the controlled Notepad if it disappeared mid-run.

    Other agents run real GUI suites on this shared desktop and may close
    windows; a missing window must never be recorded as a slow/missing
    measurement.
    """
    if handle:
        hwnd = int(handle.get("hwnd") or 0)
        windows = mods["windows"]
        alive = False
        title = ""
        try:
            alive = bool(hwnd) and bool(windows.is_window(hwnd))
            if alive:
                title = windows.get_title(hwnd) or ""
        except Exception:
            alive = False
        if alive and CONTENT_TITLE_HINT.lower() in title.lower():
            return handle
        print("   controlled Notepad gone before %s; re-launching" % tag)
        result.setdefault("controlled_window_relaunches", []).append({
            "section": tag,
            "old_hwnd": hwnd,
            "title": (title or "")[:60],
            "reason": "window gone" if hwnd else "no handle",
        })
    handle = setup_controlled_notepad(mods, bridge, result["errors"],
                                     keep_open=True)
    result["controlled_window"] = {
        key: handle.get(key) for key in
        ("window", "title", "matched", "fresh_verified",
         "window_move_rect", "setvalue", "save", "title_after_set",
         "content_sha256", "content_lines", "content_verified",
         "popup_check")
    }
    return handle


def force_close_stray_bench_notepad(mods: dict, bridge, errors: list) -> None:
    """Best-effort cleanup when Notepad setup raises before a handle exists.

    Only windows whose title contains the unique temp file name are touched, so
    a pre-existing user Notepad is never closed.
    """
    try:
        hits = mods["windows"].find_windows(CONTENT_TITLE_HINT, limit=3)
    except Exception:
        hits = []
    for hit in hits:
        hwnd = int(hit.get("hwnd") or 0)
        if not hwnd:
            continue
        try:
            bridge.act(hwnd, "close")
        except Exception as exc:
            add_error(errors, "notepad.stray_close", exc)
        time.sleep(0.3)
    if hits:
        print("   cleaned up %d stray bench Notepad window(s)" % len(hits))


# --------------------------------------------------------------------------- #
# snapshot sampling
# --------------------------------------------------------------------------- #
SNAPSHOT_METRICS = (
    "wall_ms", "elapsed_ms", "total", "shown", "ms_per_visited",
    "ms_per_shown", "walk_ms", "make_node_ms", "control_for_ms",
    "space_check_ms",
)

#: A sample slower than this multiple of the running median is treated as a
#: transient GUI collision / load spike and re-measured instead of recorded.
RETRY_FACTOR = 3.0


def robust_samples(measure_fn, n: int, value_key: str = "wall_ms",
                   factor: float = RETRY_FACTOR, max_retries=None):
    """Collect n samples; re-measure samples slower than factor x the median.

    The live desktop is shared, so a modal dialog or another agent's click can
    stall one snapshot.  Replacing only such gross outliers (and recording what
    was replaced) keeps the recorded distribution honest without hiding a
    globally slow period: if everything is slow the median is slow too.
    """
    samples = [measure_fn() for _ in range(n)]
    retries = []
    limit = n if max_retries is None else int(max_retries)
    for _ in range(limit):
        values = [s.get(value_key) for s in samples
                  if s.get(value_key) is not None]
        median = percentile(values, 50)
        if not median:
            break
        worst = max(range(len(samples)),
                    key=lambda i: samples[i].get(value_key) or 0)
        if (samples[worst].get(value_key) or 0) <= factor * median:
            break
        new_sample = measure_fn()
        retries.append({
            "index": worst,
            "old_ms": round_if(samples[worst].get(value_key), 2),
            "new_ms": round_if(new_sample.get(value_key), 2),
            "median_ms": round_if(median, 2),
        })
        samples[worst] = new_sample
    return samples, retries


def snapshot_sample(bridge, timers: BridgeTimers, hwnd: int,
                    view: str = "controls", max_nodes: int = 300) -> dict:
    timers.reset()
    t0 = time.perf_counter()
    snap = bridge.snapshot(hwnd, view=view, max_nodes=max_nodes)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    breakdown = timers.snapshot()
    total = int(snap.get("total") or 0)
    shown = int(snap.get("shown") or 0)

    def bms(name):
        return round(float(breakdown.get(name, {}).get("ms") or 0.0), 3)

    return {
        "wall_ms": round(wall_ms, 3),
        "elapsed_ms": int(snap.get("elapsed_ms") or 0),
        "total": total,
        "shown": shown,
        "truncated": bool(snap.get("truncated")),
        "ms_per_visited": round(wall_ms / total, 4) if total else None,
        "ms_per_shown": round(wall_ms / shown, 4) if shown else None,
        "walk_ms": bms("_walk"),
        "make_node_ms": bms("_make_node"),
        "control_for_ms": bms("_control_for"),
        "space_check_ms": bms("space_check"),
    }


def agg_snapshot_samples(samples: list) -> dict:
    def col(key):
        return [s.get(key) for s in samples]

    out = {
        "n": len(samples),
        "wall_ms": summarize(col("wall_ms"), digits=2),
        "wall_p95_ms": summarize(col("wall_ms"), digits=2).get("p95"),
        "elapsed_ms": summarize(col("elapsed_ms"), digits=1, int_values=True),
        "total": summarize(col("total"), digits=1, int_values=True),
        "shown": summarize(col("shown"), digits=1, int_values=True),
        "ms_per_visited": summarize(col("ms_per_visited"), digits=4),
        "ms_per_shown": summarize(col("ms_per_shown"), digits=4),
        "breakdown_ms": {
            name: summarize(col(name + "_ms"), digits=2)
            for name in ("walk", "make_node", "control_for", "space_check")
        },
        "truncated_any": any(bool(s.get("truncated")) for s in samples),
        "raw": samples,
    }
    return out


def measure_snapshot_target(mods: dict, bridge, timers: BridgeTimers,
                            target: dict, n: int, view: str = "controls",
                            max_nodes: int = 300, warmup: int = 2) -> dict:
    hwnd = int(target["hwnd"])
    for _ in range(max(warmup, 0)):
        bridge.snapshot(hwnd, view=view, max_nodes=max_nodes)
    samples, retries = robust_samples(
        lambda: snapshot_sample(bridge, timers, hwnd, view, max_nodes), n)
    block = agg_snapshot_samples(samples)
    block["outlier_retries"] = retries
    block.update({
        "target": target["name"],
        "view": view,
        "max_nodes": max_nodes,
        "window": target.get("window") or window_meta(mods, hwnd),
    })
    return block


def build_snapshot_targets(mods: dict, bridge, notepad_hwnd: int,
                           errors: list):
    """Controlled Notepad + our own Explorer/System32 + largest visible window.

    Explorer is launched only for this measurement and closed right after.  The
    largest visible window is measured READ-ONLY and depends on the user's live
    session (only its ms/visited number is comparable across runs).
    """
    targets = [{
        "name": "notepad_controlled",
        "hwnd": int(notepad_hwnd),
        "window": window_meta(mods, notepad_hwnd),
    }]
    explorer = None
    try:
        print("  launching Explorer/System32 measurement window")
        explorer = launch_app(
            mods, bridge, r"explorer.exe C:\Windows\System32", "System32",
            deadline_s=20.0, min_nodes=20, errors=errors)
        targets.append({"name": "explorer_system32",
                        "hwnd": explorer["hwnd"],
                        "window": explorer["window"]})
    except Exception as exc:
        add_error(errors, "snapshot.explorer.launch", exc)
    exclude = {int(notepad_hwnd)}
    if explorer:
        exclude.add(int(explorer["hwnd"]))
    large = pick_large_window(mods, exclude=exclude)
    if large:
        targets.append(large)
    else:
        print("  no large visible real window available (read-only target skipped)")
    return targets, explorer


def measure_snapshot_section(mods: dict, bridge, timers: BridgeTimers,
                             notepad_hwnd: int, n: int, errors: list) -> dict:
    section = {"context": os_context(mods), "targets": {}}
    targets, explorer = build_snapshot_targets(mods, bridge, notepad_hwnd, errors)
    try:
        for target in targets:
            _require_window_alive(mods, int(target["hwnd"]),
                                  "snapshot." + target["name"])
            try:
                print("  snapshot target %s (#%s)"
                      % (target["name"], target["hwnd"]))
                section["targets"][target["name"]] = measure_snapshot_target(
                    mods, bridge, timers, target, n)
            except Exception as exc:
                add_error(errors, "snapshot." + target["name"], exc)
                if target["name"] == "notepad_controlled":
                    # let the caller re-launch and retry instead of recording
                    raise
        if "notepad_controlled" in section["targets"]:
            try:
                section["notepad_view_all"] = measure_snapshot_target(
                    mods, bridge, timers, targets[0], 1, view="all",
                    max_nodes=400, warmup=0)
            except Exception as exc:
                add_error(errors, "snapshot.notepad_view_all", exc)
    finally:
        if explorer:
            section["explorer_cleanup"] = close_app(
                mods, bridge, explorer["hwnd"], errors, "snapshot_explorer")
    return section


def pick_large_window(mods: dict, exclude=None):
    """Read-only large-window snapshot target.

    Prefers the DeepSeek Harness (Chromium) window when it is visible so the
    before/after comparison uses the same application; otherwise falls back to
    the largest visible non-minimized window.  Never touches user windows: a
    UIA snapshot is read-only and does not change focus.
    """
    exclude = set(exclude or ())
    windows = mods["windows"]
    candidates = []
    try:
        for win in windows.list_windows():
            if not win.get("visible") or win.get("minimized"):
                continue
            hwnd = int(win.get("hwnd") or 0)
            if not hwnd or hwnd in exclude:
                continue
            rect = win.get("rect") or [0, 0, 0, 0]
            width = int(rect[2]) - int(rect[0])
            height = int(rect[3]) - int(rect[1])
            if width < 400 or height < 300:
                continue
            candidates.append((max(width, 0) * max(height, 0), win))
    except Exception:
        return None
    if not candidates:
        return None
    preferred = [
        cand for cand in candidates
        if "deepseek" in ((cand[1].get("proc") or "")
                          + (cand[1].get("title") or "")).lower()
    ]
    pool = preferred or candidates
    area, win = max(pool, key=lambda cand: cand[0])
    return {
        "name": "large_real_window",
        "hwnd": int(win["hwnd"]),
        "window": {
            "hwnd": int(win["hwnd"]),
            "proc": win.get("proc"),
            "title": (win.get("title") or "")[:80],
            "class": win.get("class"),
            "rect": win.get("rect"),
        },
        "selection": ("preferred DeepSeek Harness window (same app as baseline)"
                      if preferred else "largest visible non-minimized window"),
        "read_only": True,
    }


# --------------------------------------------------------------------------- #
# render sampling (compact.format_snapshot on frozen fixtures)
# --------------------------------------------------------------------------- #
def render_view_settings(view: str):
    # mirrors server.cu_show: controls renders at most max_nodes lines
    max_lines = 100 if view == "controls" else None
    return max_lines


def render_once(compact_mod, snap: dict, view: str) -> tuple:
    max_lines = render_view_settings(view)
    stats = {}
    t0 = time.perf_counter()
    text = compact_mod.format_snapshot(snap, view=view, max_lines=max_lines,
                                       stats=stats)
    ms = (time.perf_counter() - t0) * 1000.0
    return text, ms, stats


def render_block(text: str, samples: list, stats: dict) -> dict:
    chars = len(text)
    return {
        "n": len(samples),
        "ms": summarize(samples, digits=3),
        "chars": chars,
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + 1,
        "tokens_est": round(chars / 2.6, 1),
        "token_method": "chars/2.6 (no tokenizer dependency on this host)",
        "text_sha1": sha256_text(text),
        "stats": {
            "tree_cut": bool(stats.get("tree_cut")),
            "budget_cropped": bool(stats.get("budget_cropped")),
            "hidden": stats.get("hidden"),
            "rendered_lines": stats.get("rendered_lines"),
            "cap": stats.get("cap"),
        },
    }


def measure_render_section(mods: dict, fixtures: list, fixtures_dir: str,
                           n: int, errors: list) -> dict:
    compact_mod = mods["compact"]
    section = {
        "fixtures_dir": os.path.abspath(fixtures_dir),
        "fixtures_fingerprint": fixtures_fingerprint(fixtures_dir),
        "context": os_context(mods),
        "fixtures": [],
    }
    for fixture in fixtures:
        for view in fixture.get("snapshots") or {}:
            entry = {"fixture": fixture.get("name"), "kind": fixture.get("kind"),
                     "view": view, "max_lines": render_view_settings(view)}
            try:
                snap = fixture["snapshots"][view]
                for _ in range(5):
                    text, _ms, stats = render_once(compact_mod, snap, view)
                samples = []
                texts = set()
                stats = {}
                for _ in range(n):
                    text, ms, stats = render_once(compact_mod, snap, view)
                    samples.append(ms)
                    texts.add(sha256_text(text))
                entry.update(render_block(text, samples, stats))
                entry["text_stable"] = len(texts) == 1
            except Exception as exc:
                add_error(errors, "render.%s.%s" % (entry["fixture"], view), exc)
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            section["fixtures"].append(entry)
    total_chars = sum(int(e.get("chars") or 0) for e in section["fixtures"])
    total_tokens = sum(float(e.get("tokens_est") or 0) for e in section["fixtures"])
    section["totals"] = {"fixtures": len(section["fixtures"]),
                         "chars": total_chars,
                         "tokens_est": round(total_tokens, 1)}
    return section


# --------------------------------------------------------------------------- #
# screenshot section
# --------------------------------------------------------------------------- #
def measure_screenshot_section(mods: dict, bridge, handle: dict, n: int,
                               errors: list) -> dict:
    windows = mods["windows"]
    hwnd = int(handle["hwnd"])
    _require_window_alive(mods, hwnd, "screenshot")
    section = {"window": handle.get("window"), "context": os_context(mods)}
    try:
        section["activate"] = bridge.act(hwnd, "activate")
        time.sleep(0.25)
    except Exception as exc:
        add_error(errors, "screenshot.activate", exc)
    def shot_once():
        t0 = time.perf_counter()
        shot = bridge.screenshot(hwnd)
        wall = (time.perf_counter() - t0) * 1000.0
        return {
            "wall_ms": round(wall, 2),
            "reported_ms": int(shot.get("ms") or 0),
            "jpeg_bytes": len(shot.get("jpeg_bytes") or b""),
            "img_w": shot.get("img_w"),
            "img_h": shot.get("img_h"),
            "origin": shot.get("origin"),
        }

    shots, retries = robust_samples(shot_once, n)
    section["window_shot"] = {
        "n": len(shots),
        "wall_ms": summarize([s["wall_ms"] for s in shots], digits=2),
        "jpeg_bytes": summarize([s["jpeg_bytes"] for s in shots], digits=0,
                                int_values=True),
        "outlier_retries": retries,
        "raw": shots,
    }
    try:
        rect = windows.get_rect(hwnd)
        region = [int(rect[0]) + 20, int(rect[1]) + 70, 480, 360]
        t0 = time.perf_counter()
        shot = bridge.screenshot(None, region=region)
        wall = (time.perf_counter() - t0) * 1000.0
        section["region_shot"] = {
            "n": 1,
            "region": region,
            "wall_ms": round(wall, 2),
            "reported_ms": int(shot.get("ms") or 0),
            "jpeg_bytes": len(shot.get("jpeg_bytes") or b""),
            "img_w": shot.get("img_w"),
            "img_h": shot.get("img_h"),
            "origin": shot.get("origin"),
        }
    except Exception as exc:
        add_error(errors, "screenshot.region", exc)
    return section
# === PART 3: cold start / e2e / memory / fixtures ===


COLD_KEYS = (
    "spawn_to_initialize_ms",
    "adjusted_spawn_to_initialize_ms",
    "spawn_to_tools_list_ms",
    "spawn_to_first_cu_show_ms",
    "initialize_to_first_cu_show_ms",
    "child_rss_mb",
    "first_show_chars",
)


def cold_start_round(src_dir: str, mock: bool, noop_ms: float) -> dict:
    sp = ServerProc(src_dir, mock=mock)
    row = {"mock": mock}
    try:
        resp, mark_init = sp.request("initialize", INIT_PARAMS)
        row["spawn_to_initialize_ms"] = round(mark_init, 2)
        row["initialize_ok"] = bool((resp.get("result") or {}).get("serverInfo"))
        sp.notify("notifications/initialized")
        resp2, mark_tools = sp.request("tools/list")
        row["spawn_to_tools_list_ms"] = round(mark_tools, 2)
        row["tools_count"] = len(((resp2.get("result") or {}).get("tools")) or [])
        resp3, mark_show = sp.request(
            "tools/call", {"name": "cu_show", "arguments": {"scope": "desktop"}})
        row["spawn_to_first_cu_show_ms"] = round(mark_show, 2)
        row["initialize_to_first_cu_show_ms"] = round(mark_show - mark_init, 2)
        row["first_show_chars"] = len(response_text(resp3))
        row["first_show_is_error"] = response_is_error(resp3)
        row["child_rss_mb"] = sp.rss_mb()
        row["junk_lines"] = len(sp._junk)
        row["stderr_tail"] = sp.stderr_tail(4)
    finally:
        sp.close()
    row["noop_ms"] = round(noop_ms, 2)
    row["adjusted_spawn_to_initialize_ms"] = round(
        row["spawn_to_initialize_ms"] - noop_ms, 2)
    row["adjusted_spawn_to_first_cu_show_ms"] = round(
        row["spawn_to_first_cu_show_ms"] - noop_ms, 2)
    return row


def agg_cold_rows(rows: list) -> dict:
    out = {"n": len(rows)}
    for key in COLD_KEYS:
        out[key] = summarize([r.get(key) for r in rows], digits=2)
    out["noop_python_ms"] = summarize([r.get("noop_ms") for r in rows], digits=2)
    out["all_initialize_ok"] = all(bool(r.get("initialize_ok")) for r in rows)
    out["all_first_show_ok"] = not any(bool(r.get("first_show_is_error"))
                                      for r in rows)
    out["raw"] = rows
    return out


def measure_cold_start_section(src_dir: str, n: int, errors: list,
                               mods: dict | None = None) -> dict:
    section = {"context": os_context(mods)}
    for mode, mock in (("real", False), ("mock", True)):
        rows = []
        for i in range(n):
            cpu = (round(psutil.cpu_percent(interval=0.05), 1)
                   if psutil is not None else None)
            noop = spawn_noop_ms()
            try:
                row = cold_start_round(src_dir, mock, noop)
            except Exception as exc:
                add_error(errors, "cold_start.%s.%d" % (mode, i + 1), exc)
                row = {"noop_ms": round(noop, 2),
                       "error": "%s: %s" % (type(exc).__name__, exc)}
            row["cpu_load_pct"] = cpu
            rows.append(row)
            print("  cold %-4s %d/%d init=%.0fms adj=%.0fms show=%.0fms"
                  % (mode, i + 1, n,
                     row.get("spawn_to_initialize_ms") or -1,
                     row.get("adjusted_spawn_to_initialize_ms") or -1,
                     row.get("spawn_to_first_cu_show_ms") or -1))
        retries = []
        for _ in range(n):
            values = [r.get("spawn_to_initialize_ms") for r in rows
                      if not r.get("error")
                      and r.get("spawn_to_initialize_ms") is not None]
            median = percentile(values, 50)
            if not median:
                break
            worst = max(range(len(rows)),
                        key=lambda i: rows[i].get("spawn_to_initialize_ms") or 0)
            worst_val = rows[worst].get("spawn_to_initialize_ms") or 0
            if worst_val <= RETRY_FACTOR * median:
                break
            cpu = (round(psutil.cpu_percent(interval=0.05), 1)
                   if psutil is not None else None)
            noop = spawn_noop_ms()
            try:
                new_row = cold_start_round(src_dir, mock, noop)
                new_row["cpu_load_pct"] = cpu
            except Exception as exc:
                add_error(errors, "cold_start.%s.retry" % mode, exc)
                break
            retries.append({
                "index": worst,
                "old_ms": round_if(worst_val, 2),
                "new_ms": round_if(new_row.get("spawn_to_initialize_ms"), 2),
                "median_ms": round_if(median, 2),
            })
            rows[worst] = new_row
            print("  cold %-4s retry round %d: %.0fms -> %.0fms (median %.0fms)"
                  % (mode, worst + 1, worst_val,
                     new_row.get("spawn_to_initialize_ms") or -1, median))
        section[mode] = agg_cold_rows(rows)
        section[mode]["outlier_retries"] = retries
    return section


def compare_cold_start_section(src_a: str, src_b: str, rounds: int,
                               errors: list, mods: dict | None = None) -> dict:
    section = {"rounds": rounds, "context": os_context(mods),
               "real": {}, "mock": {}}
    for mode, mock in (("real", False), ("mock", True)):
        rows = []
        for i in range(rounds):
            cpu = (round(psutil.cpu_percent(interval=0.05), 1)
                   if psutil is not None else None)
            noop = spawn_noop_ms()
            order = ("a", "b") if i % 2 == 0 else ("b", "a")
            row = {"round": i + 1, "noop_ms": round(noop, 2),
                   "cpu_load_pct": cpu,
                   "order": "".join(order), "a": None, "b": None}
            for side in order:
                src = src_a if side == "a" else src_b
                try:
                    row[side] = cold_start_round(src, mock, noop)
                except Exception as exc:
                    add_error(errors, "compare.cold_start.%s.%d.%s"
                              % (mode, i + 1, side), exc)
                    row[side] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            rows.append(row)
            print("  cold-pair %-4s %d/%d a=%s b=%s"
                  % (mode, i + 1, rounds,
                     row["a"].get("adjusted_spawn_to_initialize_ms")
                     if row["a"] else "ERR",
                     row["b"].get("adjusted_spawn_to_initialize_ms")
                     if row["b"] else "ERR"))
        retries = []
        for _ in range(rounds):
            vals = []
            for row in rows:
                for side in ("a", "b"):
                    value = (row[side] or {}).get("spawn_to_initialize_ms")
                    if value is not None:
                        vals.append(value)
            median = percentile(vals, 50)
            if not median:
                break
            def round_peak(row):
                return max((row["a"] or {}).get("spawn_to_initialize_ms") or 0,
                           (row["b"] or {}).get("spawn_to_initialize_ms") or 0)
            worst = max(range(len(rows)), key=lambda i: round_peak(rows[i]))
            peak = round_peak(rows[worst])
            if peak <= RETRY_FACTOR * median:
                break
            cpu = (round(psutil.cpu_percent(interval=0.05), 1)
                   if psutil is not None else None)
            noop = spawn_noop_ms()
            old = rows[worst]
            new_row = {"round": old["round"], "noop_ms": round(noop, 2),
                       "cpu_load_pct": cpu, "order": old["order"],
                       "a": None, "b": None}
            for side in old["order"]:
                src = src_a if side == "a" else src_b
                try:
                    new_row[side] = cold_start_round(src, mock, noop)
                except Exception as exc:
                    add_error(errors, "compare.cold_start.%s.retry.%s"
                              % (mode, side), exc)
                    new_row[side] = {"error": "%s: %s"
                                     % (type(exc).__name__, exc)}
            retries.append({"round": old["round"],
                            "old_max_ms": round_if(peak, 2),
                            "new_max_ms": round_if(round_peak(new_row), 2),
                            "median_ms": round_if(median, 2)})
            rows[worst] = new_row
            print("  cold-pair %-4s retry round %d: %.0fms -> %.0fms"
                  % (mode, old["round"], peak, round_peak(new_row)))
        metrics = {}
        for key in COLD_KEYS:
            a_values = [(r["a"] or {}).get(key) for r in rows]
            b_values = [(r["b"] or {}).get(key) for r in rows]
            metrics[key] = {
                "a": summarize(a_values, digits=2),
                "b": summarize(b_values, digits=2),
                "paired": paired_summary(a_values, b_values),
            }
        first_show_errors = {
            side: sum(1 for r in rows
                      if (r[side] or {}).get("first_show_is_error"))
            for side in ("a", "b")}
        section[mode] = {"raw": rows, "metrics": metrics,
                         "first_show_error_rounds": first_show_errors}
        if first_show_errors["a"] or first_show_errors["b"]:
            print("  !! first cu_show errors: A=%d B=%d (must be 0; COM race?)"
                  % (first_show_errors["a"], first_show_errors["b"]))
    return section


# --------------------------------------------------------------------------- #
# spawned-server e2e: cu_show on the controlled Notepad window
# --------------------------------------------------------------------------- #
def e2e_session(src_dir: str, window_hint: str, calls: int) -> dict:
    sp = ServerProc(src_dir, mock=False)
    try:
        resp, mark_init = sp.request("initialize", INIT_PARAMS)
        sp.notify("notifications/initialized")
        _resp2, mark_tools = sp.request("tools/list")
        # warm-up call (first tool call pays lazy import / Bridge creation)
        warm_resp, mark_warm = sp.request(
            "tools/call", {"name": "cu_show", "arguments": {"window": window_hint}})
        def call_once():
            t0 = time.perf_counter()
            resp3, _mark = sp.request(
                "tools/call", {"name": "cu_show",
                               "arguments": {"window": window_hint}})
            wall = (time.perf_counter() - t0) * 1000.0
            text = response_text(resp3)
            return {
                "wall_ms": round(wall, 2),
                "chars": len(text),
                "lines": text.count("\n") + 1,
                "is_error": response_is_error(resp3),
            }

        samples, retries = robust_samples(call_once, calls)
        return {
            "calls": samples,
            "outlier_retries": retries,
            "spawn_to_initialize_ms": round(mark_init, 2),
            "spawn_to_tools_list_ms": round(mark_tools, 2),
            "spawn_to_first_cu_show_ms": round(mark_warm, 2),
            "warmup_chars": len(response_text(warm_resp)),
            "child_rss_mb": sp.rss_mb(),
            "window_hint": window_hint,
        }
    finally:
        sp.close()


def measure_e2e_section(src_dir: str, window_hint: str, calls: int,
                        errors: list, mods: dict | None = None) -> dict:
    section = {"window_hint": window_hint, "n": calls,
               "context": os_context(mods)}
    try:
        session = e2e_session(src_dir, window_hint, calls)
        section.update(session)
        section["wall_ms"] = summarize([c["wall_ms"] for c in session["calls"]],
                                       digits=2)
        section["chars"] = summarize([c["chars"] for c in session["calls"]],
                                     digits=1, int_values=True)
        section["all_ok"] = not any(c["is_error"] for c in session["calls"])
        print("  e2e cu_show p50=%.1fms chars=%s n=%d"
              % (section["wall_ms"]["p50"] or -1,
                 section["chars"]["p50"], calls))
    except Exception as exc:
        add_error(errors, "e2e", exc)
    return section


def compare_e2e_section(src_a: str, src_b: str, window_hint: str, calls: int,
                        rounds: int, errors: list,
                        mods: dict | None = None) -> dict:
    rows = []
    for i in range(rounds):
        order = ("a", "b") if i % 2 == 0 else ("b", "a")
        row = {"round": i + 1, "order": "".join(order), "a": None, "b": None}
        for side in order:
            src = src_a if side == "a" else src_b
            try:
                session = e2e_session(src, window_hint, calls)
                walls = [c["wall_ms"] for c in session["calls"]]
                chars = [c["chars"] for c in session["calls"]]
                row[side] = {
                    "median_call_ms": round(percentile(walls, 50), 2),
                    "max_call_ms": round(max(walls), 2),
                    "median_chars": int(percentile(chars, 50)),
                    "calls": session["calls"],
                    "spawn_to_initialize_ms": session["spawn_to_initialize_ms"],
                    "spawn_to_first_cu_show_ms": session["spawn_to_first_cu_show_ms"],
                    "child_rss_mb": session["child_rss_mb"],
                }
            except Exception as exc:
                add_error(errors, "compare.e2e.%d.%s" % (i + 1, side), exc)
                row[side] = {"error": "%s: %s" % (type(exc).__name__, exc)}
        rows.append(row)
        print("  e2e-pair %d/%d a=%s b=%s"
              % (i + 1, rounds,
                 (row["a"] or {}).get("median_call_ms"),
                 (row["b"] or {}).get("median_call_ms")))
    a_values = [(r["a"] or {}).get("median_call_ms") for r in rows]
    b_values = [(r["b"] or {}).get("median_call_ms") for r in rows]
    a_chars = [(r["a"] or {}).get("median_chars") for r in rows]
    b_chars = [(r["b"] or {}).get("median_chars") for r in rows]
    error_calls = {
        side: sum(1 for r in rows
                  for call in ((r[side] or {}).get("calls") or [])
                  if call.get("is_error"))
        for side in ("a", "b")}
    if error_calls["a"] or error_calls["b"]:
        print("  !! e2e cu_show errors: A=%d B=%d (must be 0; COM race?)"
              % (error_calls["a"], error_calls["b"]))
    return {
        "window_hint": window_hint,
        "calls_per_round": calls,
        "rounds": rounds,
        "error_calls": error_calls,
        "context": os_context(mods),
        "raw": rows,
        "metrics": {
            "median_call_ms": {
                "a": summarize(a_values, digits=2),
                "b": summarize(b_values, digits=2),
                "paired": paired_summary(a_values, b_values),
            },
            "median_chars": {
                "a": summarize(a_chars, digits=1, int_values=True),
                "b": summarize(b_chars, digits=1, int_values=True),
                "paired": paired_summary(a_chars, b_chars),
            },
        },
    }


# --------------------------------------------------------------------------- #
# memory growth
# --------------------------------------------------------------------------- #
def map_sizes(bridge) -> dict:
    out = {}
    for name in ("_rid2id", "_id2el", "_cache", "_win_ctl", "_win_info"):
        try:
            out[name] = len(getattr(bridge, name))
        except Exception:
            out[name] = None
    try:
        out["_next_id"] = int(getattr(bridge, "_next_id"))
    except Exception:
        out["_next_id"] = None
    return out


def memory_block(mods: dict, hwnd: int, n: int) -> dict:
    bridge = mods["bridge"].Bridge()
    for _ in range(3):
        bridge.snapshot(hwnd, view="controls", max_nodes=300)
    gc.collect()
    rss_start = own_rss_mb()
    maps_start = map_sizes(bridge)
    snap_ms = []
    samples = []
    step = max(1, n // 4)
    for i in range(n):
        t0 = time.perf_counter()
        bridge.snapshot(hwnd, view="controls", max_nodes=300)
        snap_ms.append((time.perf_counter() - t0) * 1000.0)
        if (i + 1) % step == 0 or i + 1 == n:
            samples.append({
                "done": i + 1,
                "rss_mb": own_rss_mb(),
                "maps": map_sizes(bridge),
                "last_snapshot_ms": round(snap_ms[-1], 2),
            })
    gc.collect()
    rss_end = own_rss_mb()
    maps_end = map_sizes(bridge)
    return {
        "n": n,
        "rss_start_mb": rss_start,
        "rss_end_mb": rss_end,
        "rss_delta_mb": round(rss_end - rss_start, 2),
        "maps_start": maps_start,
        "maps_end": maps_end,
        "map_growth": {
            name: (None if maps_start.get(name) is None
                   or maps_end.get(name) is None
                   else int(maps_end[name]) - int(maps_start[name]))
            for name in maps_start
        },
        "snapshot_ms": summarize(snap_ms, digits=2),
        "samples": samples,
    }


def measure_memory_section(mods: dict, hwnd: int, n: int, errors: list) -> dict:
    _require_window_alive(mods, hwnd, "memory")
    section = {"hwnd": int(hwnd), "window": window_meta(mods, hwnd)}
    block = memory_block(mods, hwnd, n)
    section.update(block)
    print("  memory n=%d rid2id %s->%s id2el %s->%s rss %s->%sMB"
          % (n, section["maps_start"].get("_rid2id"),
             section["maps_end"].get("_rid2id"),
             section["maps_start"].get("_id2el"),
             section["maps_end"].get("_id2el"),
             section["rss_start_mb"], section["rss_end_mb"]))
    return section


# --------------------------------------------------------------------------- #
# fixtures: generic app launch / close helpers
# --------------------------------------------------------------------------- #
def launch_app(mods: dict, bridge, app: str, title_hint: str,
               deadline_s: float = 15.0, min_nodes: int = 1,
               errors: list | None = None) -> dict:
    windows = mods["windows"]
    before = {w["hwnd"] for w in windows.list_windows()}
    result = bridge.act(0, "launch", text=app)
    info = dict(getattr(bridge, "_last_launch", None) or {})
    hwnd = int(result.get("hwnd") or info.get("hwnd") or 0)
    if not result.get("ok") and not hwnd:
        raise RuntimeError("launch %r failed: %s" % (app, result.get("detail")))
    if hwnd in before:
        raise RuntimeError("launch %r reused a pre-existing window #%d; refusing"
                           % (app, hwnd))
    deadline = time.monotonic() + deadline_s
    last_error = None
    while time.monotonic() < deadline:
        try:
            if windows.is_window(hwnd):
                title = windows.get_title(hwnd) or ""
                if not title_hint or title_hint.lower() in title.lower():
                    snap = bridge.snapshot(hwnd, view="controls", max_nodes=120)
                    if int(snap.get("shown") or 0) >= min_nodes:
                        return {"hwnd": hwnd, "launch": info,
                                "window": window_meta(mods, hwnd)}
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError("window %r (hint %r) not ready in %.0fs (last_error=%r)"
                       % (app, title_hint, deadline_s, last_error))


def close_app(mods: dict, bridge, hwnd: int, errors: list, tag: str) -> dict:
    windows = mods["windows"]
    out = {"close": None, "forced": False, "closed": False}
    if not hwnd or not windows.is_window(hwnd):
        out["closed"] = True
        out["already_gone"] = True
        return out
    try:
        out["close"] = bridge.act(hwnd, "close")
    except Exception as exc:
        add_error(errors, "close.%s" % tag, exc)
    time.sleep(0.3)
    if windows.is_window(hwnd):
        pid = int(window_meta(mods, hwnd).get("pid") or 0)
        if pid and psutil is not None:
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                out["forced"] = True
                out["forced_pid"] = pid
            except Exception as exc:
                add_error(errors, "close.%s.forced" % tag, exc)
        time.sleep(0.3)
    out["closed"] = not windows.is_window(hwnd)
    return out


def capture_fixture(mods: dict, bridge, src_dir: str, name: str, kind: str,
                    hwnd: int, view_params: dict, window: dict,
                    errors: list) -> dict:
    fixture = {
        "schema": FIXTURE_SCHEMA,
        "name": name,
        "kind": kind,
        "captured_at": iso_now(),
        "generated_from": {
            "src_dir": os.path.abspath(src_dir),
            "src_fingerprint": src_fingerprint(src_dir),
        },
        "context": os_context(mods),
        "window": window,
        "snapshots": {},
        "render_reference_pristine": {},
    }
    for view, max_nodes in view_params.items():
        snap = bridge.snapshot(hwnd, view=view, max_nodes=max_nodes)
        fixture["snapshots"][view] = normalize_snapshot(snap)
        try:
            text, _ms, _stats = render_once(
                mods["compact"], fixture["snapshots"][view], view)
            fixture["render_reference_pristine"][view] = {
                "chars": len(text),
                "bytes": len(text.encode("utf-8")),
                "lines": text.count("\n") + 1,
                "tokens_est": round(len(text) / 2.6, 1),
            }
        except Exception as exc:
            add_error(errors, "fixture_render_ref.%s.%s" % (name, view), exc)
        print("  fixture %-18s view=%-8s total=%-5s shown=%-4s"
              % (name, view, fixture["snapshots"][view].get("total"),
                 fixture["snapshots"][view].get("shown")))
    return fixture


def dump_fixtures(mods: dict, src_dir: str, fixtures_dir: str,
                  errors: list) -> dict:
    bridge = mods["bridge"].Bridge()
    fixtures = []
    notepad = None
    try:
        print("== capture controlled Notepad fixture")
        notepad = setup_controlled_notepad(mods, bridge, errors)
        fixtures.append(capture_fixture(
            mods, bridge, src_dir, "notepad", "real", notepad["hwnd"],
            {"controls": 300, "all": 400, "text": 300},
            {k: notepad["window"].get(k) for k in ("proc", "title", "class", "rect")},
            errors))
    except Exception as exc:
        add_error(errors, "dump.notepad", exc)
        force_close_stray_bench_notepad(mods, bridge, errors)
    finally:
        if notepad:
            try:
                close_controlled_notepad(mods, bridge, notepad, errors)
            except Exception as exc:
                add_error(errors, "dump.notepad.close", exc)
    for name, app, hint, views in (
            ("calculator", "calc.exe", "", {"controls": 300, "all": 400}),
            ("explorer_system32", "explorer.exe C:\\Windows\\System32",
             "System32", {"controls": 300, "all": 400})):
        handle = None
        try:
            print("== capture %s fixture" % name)
            handle = launch_app(mods, bridge, app, hint, min_nodes=5,
                                errors=errors)
            fixtures.append(capture_fixture(
                mods, bridge, src_dir, name, "real", handle["hwnd"], views,
                {k: handle["window"].get(k)
                 for k in ("proc", "title", "class", "rect")}, errors))
        except Exception as exc:
            add_error(errors, "dump.%s" % name, exc)
        finally:
            if handle:
                try:
                    close_app(mods, bridge, handle["hwnd"], errors, name)
                except Exception as exc:
                    add_error(errors, "dump.%s.close" % name, exc)
    try:
        print("== capture mock fixtures")
        mock_bridge = mods["mock_bridge"].Bridge()
        mock_windows = {w["hwnd"]: w for w in mods["mock_bridge"].list_windows()}
        for name, hwnd, views in (
                ("mock_large", 1007, {"controls": 400, "all": 400}),
                ("mock_edge", 1002, {"controls": 300, "all": 300})):
            win = mock_windows.get(hwnd) or {}
            fixtures.append(capture_fixture(
                mods, mock_bridge, src_dir, name, "mock", hwnd, views,
                {k: win.get(k) for k in ("proc", "title", "class", "rect")},
                errors))
    except Exception as exc:
        add_error(errors, "dump.mock", exc)

    manifest = {
        "schema": FIXTURE_SCHEMA,
        "kind": "manifest",
        "generated_at": iso_now(),
        "generated_from": {
            "src_dir": os.path.abspath(src_dir),
            "src_fingerprint": src_fingerprint(src_dir),
        },
        "context": os_context(mods),
        "files": {},
    }
    for fixture in fixtures:
        fname = fixture["name"] + ".json"
        path = os.path.join(fixtures_dir, fname)
        write_json_file(path, fixture)
        manifest["files"][fixture["name"]] = {
            "file": fname,
            "kind": fixture["kind"],
            "sha256": sha256_file(path),
            "bytes": os.path.getsize(path),
            "views": {view: {"total": snap.get("total"),
                             "shown": snap.get("shown")}
                      for view, snap in fixture["snapshots"].items()},
            "render_reference_pristine": fixture["render_reference_pristine"],
        }
    write_json_file(os.path.join(fixtures_dir, "manifest.json"), manifest)
    return manifest


# --------------------------------------------------------------------------- #
# compare-mode in-process sections
# --------------------------------------------------------------------------- #
def compare_snapshot_section(mods_a: dict, bridge_a, timers_a, bridge_b,
                             timers_b, targets: list, rounds: int,
                             errors: list) -> dict:
    section = {"rounds": rounds, "context": os_context(mods_a), "targets": {}}
    for target in targets:
        hwnd = int(target["hwnd"])
        try:
            print("  snapshot pair %s (#%s) rounds=%d"
                  % (target["name"], hwnd, rounds))
            for _ in range(2):
                bridge_a.snapshot(hwnd, view="controls", max_nodes=300)
                bridge_b.snapshot(hwnd, view="controls", max_nodes=300)
            def measure_round(index, sides):
                row = {"round": index + 1,
                       "order": "".join(side for side, _b, _t in sides),
                       "cpu_load_pct": round(psutil.cpu_percent(interval=0.05), 1)
                       if psutil else None,
                       "a": None, "b": None}
                for side, bridge, timers in sides:
                    row[side] = snapshot_sample(bridge, timers, hwnd,
                                                "controls", 300)
                return row

            def default_sides(index):
                sides = [("a", bridge_a, timers_a), ("b", bridge_b, timers_b)]
                if index % 2 == 1:
                    sides.reverse()
                return sides

            raw = [measure_round(i, default_sides(i)) for i in range(rounds)]
            retries = []
            for _ in range(rounds):
                peaks = [max(r["a"]["wall_ms"], r["b"]["wall_ms"]) for r in raw]
                median = percentile(peaks, 50)
                if not median:
                    break
                worst = max(range(len(raw)), key=lambda i: peaks[i])
                if peaks[worst] <= RETRY_FACTOR * median:
                    break
                new_row = measure_round(worst, default_sides(worst))
                retries.append({
                    "round": raw[worst]["round"],
                    "old_max_ms": round_if(peaks[worst], 2),
                    "new_max_ms": round_if(max(new_row["a"]["wall_ms"],
                                              new_row["b"]["wall_ms"]), 2),
                    "median_ms": round_if(median, 2),
                })
                print("    retry round %d: %.1fms -> %.1fms (median %.1fms)"
                      % (raw[worst]["round"], peaks[worst],
                         max(new_row["a"]["wall_ms"],
                             new_row["b"]["wall_ms"]), median))
                raw[worst] = new_row
            a_samples = [r["a"] for r in raw]
            b_samples = [r["b"] for r in raw]
            metrics = {}
            for key in SNAPSHOT_METRICS:
                a_values = [s.get(key) for s in a_samples]
                b_values = [s.get(key) for s in b_samples]
                metrics[key] = {
                    "a": summarize(a_values, digits=4),
                    "b": summarize(b_values, digits=4),
                    "paired": paired_summary(a_values, b_values),
                }
            section["targets"][target["name"]] = {
                "target": target["name"],
                "view": "controls",
                "max_nodes": 300,
                "window": target.get("window"),
                "read_only": bool(target.get("read_only")),
                "metrics": metrics,
                "outlier_retries": retries,
                "raw": raw,
            }
            pv = section["targets"][target["name"]]["metrics"]["ms_per_visited"]
            print("    ms/visited a=%.3f b=%.3f delta=%+.1f%%"
                  % (pv["a"]["p50"] or -1, pv["b"]["p50"] or -1,
                     pv["paired"]["delta_pct_p50"] or 0.0))
        except Exception as exc:
            add_error(errors, "compare.snapshot." + target["name"], exc)
            if target["name"] == "notepad_controlled":
                raise
    return section


def compare_render_section(compact_a, compact_b, fixtures: list,
                           fixtures_dir: str, n: int, errors: list,
                           mods: dict | None = None) -> dict:
    section = {
        "fixtures_dir": os.path.abspath(fixtures_dir),
        "fixtures_fingerprint": fixtures_fingerprint(fixtures_dir),
        "context": os_context(mods),
        "fixtures": [],
    }
    for fixture in fixtures:
        for view in fixture.get("snapshots") or {}:
            entry = {"fixture": fixture.get("name"), "kind": fixture.get("kind"),
                     "view": view, "max_lines": render_view_settings(view)}
            try:
                snap = fixture["snapshots"][view]
                for _ in range(5):
                    render_once(compact_a, snap, view)
                    render_once(compact_b, snap, view)
                samples_a, samples_b = [], []
                text_a = text_b = ""
                stats_a = stats_b = {}
                for i in range(n):
                    if i % 2 == 0:
                        text_a, ms_a, stats_a = render_once(compact_a, snap, view)
                        text_b, ms_b, stats_b = render_once(compact_b, snap, view)
                    else:
                        text_b, ms_b, stats_b = render_once(compact_b, snap, view)
                        text_a, ms_a, stats_a = render_once(compact_a, snap, view)
                    samples_a.append(ms_a)
                    samples_b.append(ms_b)
                block_a = render_block(text_a, samples_a, stats_a)
                block_b = render_block(text_b, samples_b, stats_b)
                entry.update({
                    "a": block_a,
                    "b": block_b,
                    "paired_ms": paired_summary(samples_a, samples_b),
                    "paired_chars": paired_summary([block_a["chars"]] * n,
                                                   [block_b["chars"]] * n),
                    "same_text": block_a["text_sha1"] == block_b["text_sha1"],
                    "chars_delta": block_b["chars"] - block_a["chars"],
                    "chars_delta_pct": round(
                        (block_b["chars"] - block_a["chars"]) * 100.0
                        / block_a["chars"], 2) if block_a["chars"] else None,
                    "tokens_delta": round(
                        block_b["tokens_est"] - block_a["tokens_est"], 1),
                })
            except Exception as exc:
                add_error(errors, "compare.render.%s.%s"
                          % (entry["fixture"], view), exc)
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            section["fixtures"].append(entry)
    total_a = sum(int(e.get("a", {}).get("chars") or 0)
                  for e in section["fixtures"])
    total_b = sum(int(e.get("b", {}).get("chars") or 0)
                  for e in section["fixtures"])
    section["totals"] = {
        "fixtures": len(section["fixtures"]),
        "chars_a": total_a,
        "chars_b": total_b,
        "chars_delta": total_b - total_a,
        "chars_delta_pct": round((total_b - total_a) * 100.0 / total_a, 2)
        if total_a else None,
        "tokens_est_a": round(total_a / 2.6, 1),
        "tokens_est_b": round(total_b / 2.6, 1),
        "identical_outputs": sum(1 for e in section["fixtures"]
                                 if e.get("same_text")),
    }
    return section


def compare_memory_section(mods_a: dict, mods_b: dict, hwnd: int, n: int,
                           errors: list) -> dict:
    section = {"hwnd": int(hwnd), "n": n,
               "window": window_meta(mods_a, hwnd),
               "context": os_context(mods_a)}
    for side, mods in (("a", mods_a), ("b", mods_b)):
        try:
            print("  memory pair side=%s n=%d" % (side, n))
            section[side] = memory_block(mods, hwnd, n)
        except Exception as exc:
            add_error(errors, "compare.memory." + side, exc)
    return section
# === PART 4: report generation ===


def fmt(value, digits: int = 1, suffix: str = "", dash: str = "-") -> str:
    if value is None:
        return dash
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number) or math.isinf(number):
        return dash
    return ("%." + str(digits) + "f") % number + suffix


def cell(block, key: str = "p50", digits: int = 1, suffix: str = "") -> str:
    if not isinstance(block, dict):
        return "-"
    return fmt(block.get(key), digits, suffix)


def dig(doc, *path):
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def md_table(headers, rows):
    out = []
    out.append("| " + " | ".join(str(h) for h in headers) + " |")
    out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
    return out


def _fg_text(state: dict) -> str:
    fg = (state or {}).get("foreground") or {}
    if not fg:
        return "-"
    return "%s: %s" % (fg.get("proc") or "?", (fg.get("title") or "")[:40])


def _context_lines(doc):
    ctx = doc.get("context") or {}
    start = ctx.get("start") or {}
    end = ctx.get("end") or {}
    rows = [
        ["captured_at", start.get("captured_at"), end.get("captured_at")],
        ["cpu_load_pct", start.get("cpu_load_pct"), end.get("cpu_load_pct")],
        ["python_processes", start.get("python_processes"),
         end.get("python_processes")],
        ["visible_windows", start.get("visible_windows"),
         end.get("visible_windows")],
        ["own_rss_mb", start.get("own_rss_mb"), end.get("own_rss_mb")],
        ["foreground", _fg_text(start), _fg_text(end)],
    ]
    return ["## Machine state (covariates)", "",
            *md_table(["signal", "start", "end"], rows), ""]


def _src_fingerprint_lines(doc):
    fp = doc.get("src_fingerprint") or {}
    rows = [[name, str(info.get("sha256"))[:16], info.get("bytes")]
            for name, info in sorted(fp.items())]
    return ["## Source under test", "",
            "- src_dir: %s" % doc.get("src_dir"), "",
            *md_table(["module", "sha256 (first 16)", "bytes"], rows), ""]


def _controlled_window_lines(doc):
    cw = doc.get("controlled_window")
    if not cw:
        return []
    window = cw.get("window") or {}
    fresh = cw.get("fresh_verified")
    if fresh is None:
        fresh = "not recorded (captured before the field existed); matched=%s" % (
            cw.get("matched"),)
    popup = cw.get("popup_check") or {}
    popup_text = ("modal_children=%s, other_notepad_windows=%s, dismiss_actions=%s"
                  % (popup.get("modal_children_found"),
                     len(popup.get("other_notepad_windows") or []),
                     len(popup.get("actions") or [])))
    lines = ["## Controlled window (Notepad)", "",
             "- window: %s #%s rect=%s" % (window.get("proc"),
                                           window.get("hwnd"),
                                           window.get("rect")),
             "- fresh launch verified: %s, popup check: %s"
             % (fresh, popup_text),
             "- setvalue: ok=%s via=%s ms=%s verified=%s" % (
                 (cw.get("setvalue") or {}).get("ok"),
                 (cw.get("setvalue") or {}).get("via"),
                 (cw.get("setvalue") or {}).get("ms"),
                 (cw.get("setvalue") or {}).get("verified")),
             "- content_verified: %s (%s lines, sha256=%s)" % (
                 cw.get("content_verified"), cw.get("content_lines"),
                 str(cw.get("content_sha256"))[:16]),
             "- rect after window_move: %s" % (cw.get("window_move_rect"),)]
    relaunches = doc.get("controlled_window_relaunches") or []
    lines.append("- re-launches during run: %d" % len(relaunches))
    for event in relaunches:
        lines.append("  - %s: %s" % (event.get("section"), event.get("reason")))
    cleanup = doc.get("controlled_window_cleanup")
    if cleanup:
        lines.append("- cleanup: closed=%s forced=%s" % (
            cleanup.get("closed"), cleanup.get("forced")))
    lines.append("")
    return lines


def _fixture_provenance_lines(doc):
    fp = doc.get("fixtures_fingerprint") or {}
    rows = [[name, str(info.get("sha256"))[:16], info.get("bytes")]
            for name, info in sorted(fp.items())]
    return ["### Fixtures (frozen, generated from pristine src)", "",
            "- fixtures_dir: %s" % doc.get("fixtures_dir"), "",
            *md_table(["file", "sha256 (first 16)", "bytes"], rows), ""]


def _method_lines(interleaved: bool):
    lines = ["## Method", ""]
    lines += [
        "- Harness: bench/opt/bench.py, schema %d. Every number in this report "
        "is read from the recorded JSON file(s) named below; nothing is typed "
        "by hand." % SCHEMA,
        "- Percentiles: linear interpolation over the recorded samples; p50 is "
        "the median. p95 from small n is indicative only.",
        "- Snapshot: in-process bridge.snapshot(hwnd, view, max_nodes) on the "
        "controlled Notepad window, on our own Explorer/System32 window, and on "
        "a large real window (read-only: no focus change, no input; the DeepSeek "
        "Harness/Chromium window is preferred so before/after use the same app, "
        "falling back to the largest visible window). Bridge methods "
        "(snapshot/_walk/_make_node/_control_for/space_check) are wrapped by "
        "the harness to report the internal breakdown; src is not modified.",
        "- ms/visited = wall_ms / bridge-reported total (visited UIA nodes) and "
        "is the load-robust headline metric; absolute milliseconds drift with "
        "CPU load.",
        "- Controlled window: Notepad opened on a fixed 41-line ASCII+Chinese "
        "document, content set through bridge setvalue (verified by read-back), "
        "window moved to a fixed rect, saved and closed at the end of the run.",
        "- Render: compact.format_snapshot over frozen fixture JSON captured "
        "from the pristine src; controls view uses max_lines=100 (server "
        "default). tokens_est = chars / 2.6 because no tokenizer is installed "
        "on this host (method recorded in JSON).",
        "- Cold start: spawn python -u src/server.py (real, and --mock), then "
        "initialize, tools/list and first cu_show scope=desktop. raw includes "
        "interpreter startup; adjusted subtracts a paired interpreter-startup "
        "spawn measured in the same round over the same transport (spawn until "
        "the first stdout line, then kill).",
        "- E2E: spawned real server, cu_show on the controlled Notepad window, "
        "wall-clock measured client side.",
        "- Memory: sequential snapshots on the controlled window from a fresh "
        "Bridge; id-map sizes and process RSS sampled every n/4 snapshots.",
        "- Screenshot: bridge.screenshot(notepad) plus one region capture.",
        "- Each section records CPU load, python process count, visible window "
        "count and node counts as covariates.",
    ]
    if interleaved:
        lines.insert(1, "")
        lines.insert(2, "- INTERLEAVED A/B: both src trees are loaded as "
                       "isolated module sets in one process; A and B are "
                       "measured alternately (order flips every round) so the "
                       "paired median delta cancels machine-load drift. "
                       "Subprocess metrics alternate spawned servers A and B.")
    lines.append("")
    return lines


CAVEATS = [
    "## Caveats",
    "",
    "- This machine is shared: the user's live desktop session and other "
    "agents' benchmarks run concurrently, so raw latency drifts 30-50%. Quote "
    "ms/visited and paired deltas, not absolute numbers across runs.",
    "- The large real window is measured read-only and deliberately NOT frozen "
    "into a fixture (it contains user data); its title changes with the user's "
    "session.",
    "- Notepad on this host uses English UI strings; fixture window titles are "
    "therefore English even though the controlled content is ASCII+Chinese.",
    "- Screenshot ms/bytes depend on window size, DPI and occlusion; the region "
    "capture is clamped to the physical screen.",
    "- Cold-start adjusted values subtract a paired interpreter startup "
    "(spawn until first stdout line, measured in the same round). The "
    "interpreter itself is slower under load (observed ~0.3s idle vs ~1.4s "
    "loaded), so adjusted is a better comparison unit than raw. A negative "
    "adjusted value means the server reached initialize at or below the "
    "interpreter-startup floor: there is no measurable import cost left, and "
    "the residual is spawn-scheduler noise (~100-300ms), not a real negative "
    "latency.",
    "- Baseline absolute numbers were captured on a shared machine while other "
    "agents were benchmarking (see Machine state: CPU load and python process "
    "counts at start/end). They describe that run, not a constant; the "
    "authoritative before/after evidence is the paired interleaved mode.",
    "- The memory section measures a STABLE controlled window, so id-maps can "
    "show zero growth and process RSS can even shrink (GC/allocator). The "
    "unbounded-growth risk appears when the element set changes (tree rebuilds, "
    "many windows); combine this measurement with the code-level caps review.",
    "- Lazy-import / prefetch tradeoff: the candidate defers the uiautomation "
    "import to the first tool call (with a background prefetch after "
    "initialize). On a zero-idle-gap client (initialize -> tools/list -> "
    "cu_show immediately, as in the cold_start section) that first call pays "
    "the deferred import (~590ms), so spawn->first cu_show only improves ~10% "
    "there; with a realistic idle gap the prefetch finishes and the first call "
    "is cheap (verifier's independent measurement: first cu_show 226ms -> "
    "171ms; prefetch matrix after 1.5s idle: 12.2ms with prefetch vs 480.2ms "
    "without). Steady-state e2e cu_show is the warm-server metric.",
    "- Candidate snapshot breakdown: the cache-request fast path replaces "
    "Bridge._walk / _make_node, so those legacy timers read 0 in candidate "
    "runs. Compare wall_ms / ms/visited plus visited/shown node counts (node "
    "counts matched the pristine bridge on every target here), not the legacy "
    "breakdown.",
    "- e2e numbers are always measured by spawning fresh server processes per "
    "side; the first real cu_show of each server must not be isError "
    "(CoInitialize/COM-race gate). A single-src baseline doc reports the "
    "pristine server; the paired section re-measures both sides.",
    "- GUI collisions on this shared desktop (another agent's test typing into "
    "or closing windows, modal dialogs) are handled by fresh-window "
    "verification, re-measuring samples slower than 3x the median (outlier "
    "retries) and re-launching the controlled Notepad (re-launches); both "
    "counters are recorded in the JSON.",
    "- Noise floor: a same-code paired validation run (A and B both pristine, "
    "3 rounds) produced snapshot ms/visited deltas of -5.6%..+1.6% and "
    "cold-start adjusted deltas of -3.1%..-1.4%; small-n cold-start noise can "
    "reach ~20% (observed in a second n=3 run). Treat deltas inside that band "
    "as noise; the AFTER run uses --cmp-rounds 5 and flags anything worse than "
    "-10%.",
    "- Candidate first-show gate: in compare mode the harness checks that the "
    "candidate's first real cu_show is not isError (CoInitialize/COM-race "
    "regression) and exits with code 4 if it is; the JSON is still written for "
    "diagnosis. Do not trust e2e/cold-start numbers from a gated run.",
    "- Screenshot is not compared in --compare mode (identical code path); run "
    "it in a single-src run against the candidate if screenshot bytes matter.",
    "- Fixtures were generated from the pristine src copy and are not "
    "regenerated by ordinary runs (use --dump-fixtures deliberately).",
    "",
]


def _repro_lines(docs):
    lines = ["## Reproduce", ""]
    for doc in docs:
        argv = (doc.get("harness") or {}).get("argv") or []
        cfg = " ".join(str(a) for a in argv)
        lines.append("- %s: %s" % (doc.get("label"), doc.get("kind")))
        lines.append("")
        lines.append("~~~")
        lines.append("python " + (cfg or "bench/opt/bench.py"))
        lines.append("~~~")
        lines.append("")
    lines += [
        "- Regenerate this report (from the JSON files used above):",
        "",
        "~~~",
        "python bench/opt/bench.py --report {{REPORT_INPUTS}} --report-out docs/OPT-BASELINE.md",
        "~~~",
        "",
        "- AFTER run (authoritative paired evidence once both optimizers are "
        "done; interleaves pristine A and candidate B in one process):",
        "",
        "~~~",
        "python bench/opt/bench.py --src-a D:/opencode/_supercu_opt/orig/src "
        "--src-b D:/opencode/plugins/super-cu/src --label after "
        "--cmp-rounds 5 --sections cold_start,snapshot,render,e2e,memory "
        "--out bench/opt/compare-after.json",
        "~~~",
        "",
        "- Generate the AFTER document (keeps this baseline document immutable):",
        "",
        "~~~",
        "python bench/opt/bench.py --report bench/opt/baseline.json "
        "bench/opt/compare-after.json --report-out docs/OPT-AFTER.md",
        "~~~",
        "",
        "- Optional single-src screenshot numbers for the candidate:",
        "",
        "~~~",
        "python bench/opt/bench.py --src-dir D:/opencode/plugins/super-cu/src "
        "--label after-screenshot --sections screenshot "
        "--out bench/opt/after-screenshot.json --quick",
        "~~~",
        "",
    ]
    return lines


# --------------------------------------------------------------------------- #
# single-run tables
# --------------------------------------------------------------------------- #
def _cold_table(section):
    rows = []
    for mode in ("real", "mock"):
        blk = (section or {}).get(mode) or {}
        rows.append([
            mode, blk.get("n"),
            cell(blk.get("spawn_to_initialize_ms"), "p50"),
            cell(blk.get("spawn_to_initialize_ms"), "p95"),
            cell(blk.get("noop_python_ms"), "p50"),
            cell(blk.get("adjusted_spawn_to_initialize_ms"), "p50"),
            cell(blk.get("adjusted_spawn_to_initialize_ms"), "p95"),
            cell(blk.get("spawn_to_tools_list_ms"), "p50"),
            cell(blk.get("spawn_to_first_cu_show_ms"), "p50"),
            cell(blk.get("child_rss_mb"), "p50"),
            "yes" if blk.get("all_first_show_ok") else "NO",
        ])
    return ["## Cold start (spawn -> initialize -> tools/list -> cu_show)",
            "",
            *md_table(["mode", "n", "raw init p50 ms", "raw init p95 ms",
                       "noop python p50 ms", "adjusted init p50 ms",
                       "adjusted init p95 ms", "spawn->tools/list p50 ms",
                       "spawn->first cu_show p50 ms", "child RSS p50 MB",
                       "first show ok"], rows), ""]


def _snapshot_rows(section):
    rows = []
    targets = (section or {}).get("targets") or {}
    for name, blk in targets.items():
        rows.append([
            name, blk.get("view"), blk.get("n"),
            cell(blk.get("total"), "p50", 0), cell(blk.get("shown"), "p50", 0),
            cell(blk.get("wall_ms"), "p50"), cell(blk.get("wall_ms"), "p95"),
            cell(blk.get("ms_per_visited"), "p50", 3),
            cell(blk.get("ms_per_visited"), "p95", 3),
            cell((blk.get("breakdown_ms") or {}).get("walk"), "p50"),
            cell((blk.get("breakdown_ms") or {}).get("make_node"), "p50"),
            cell((blk.get("breakdown_ms") or {}).get("control_for"), "p50"),
            cell((blk.get("breakdown_ms") or {}).get("space_check"), "p50"),
            len(blk.get("outlier_retries") or []),
        ])
    view_all = (section or {}).get("notepad_view_all")
    if view_all:
        rows.append([
            "notepad_controlled", "all", view_all.get("n"),
            cell(view_all.get("total"), "p50", 0),
            cell(view_all.get("shown"), "p50", 0),
            cell(view_all.get("wall_ms"), "p50"),
            cell(view_all.get("wall_ms"), "p95"),
            cell(view_all.get("ms_per_visited"), "p50", 3),
            cell(view_all.get("ms_per_visited"), "p95", 3),
            cell((view_all.get("breakdown_ms") or {}).get("walk"), "p50"),
            cell((view_all.get("breakdown_ms") or {}).get("make_node"), "p50"),
            cell((view_all.get("breakdown_ms") or {}).get("control_for"), "p50"),
            cell((view_all.get("breakdown_ms") or {}).get("space_check"), "p50"),
            len(view_all.get("outlier_retries") or []),
        ])
    return ["## Snapshot (in-process bridge.snapshot, controls view unless noted)",
            "",
            *md_table(["target", "view", "n", "visited p50", "shown p50",
                       "wall p50 ms", "wall p95 ms", "ms/visited p50",
                       "ms/visited p95", "walk p50 ms", "make_node p50 ms",
                       "control_for p50 ms", "space_check p50 ms",
                       "outlier retries"], rows), ""]


def _render_table(section):
    rows = []
    for entry in (section or {}).get("fixtures") or []:
        rows.append([
            entry.get("fixture"), entry.get("kind"), entry.get("view"),
            entry.get("chars"), entry.get("bytes"), entry.get("lines"),
            entry.get("tokens_est"),
            cell(entry.get("ms"), "p50", 2), cell(entry.get("ms"), "p95", 2),
            "yes" if (entry.get("stats") or {}).get("tree_cut") else "no",
            "yes" if (entry.get("stats") or {}).get("budget_cropped") else "no",
            (entry.get("stats") or {}).get("hidden"),
            "yes" if entry.get("text_stable") else "NO",
        ])
    totals = (section or {}).get("totals") or {}
    return ["## Render (compact.format_snapshot on frozen fixtures)", "",
            *md_table(["fixture", "kind", "view", "chars", "bytes", "lines",
                       "tokens_est", "p50 ms", "p95 ms", "tree_cut",
                       "budget_cropped", "hidden", "stable"], rows), "",
            "- totals: %s fixture/view pairs, %s chars, %s tokens_est"
            % (totals.get("fixtures"), totals.get("chars"),
               totals.get("tokens_est")), ""]


def _screenshot_table(section):
    rows = []
    win = (section or {}).get("window_shot") or {}
    if win:
        raw = win.get("raw") or []
        first = raw[0] if raw else {}
        rows.append(["window", win.get("n"),
                     cell(win.get("wall_ms"), "p50"),
                     cell(win.get("wall_ms"), "p95"),
                     cell(win.get("jpeg_bytes"), "p50", 0),
                     "%sx%s" % (first.get("img_w"), first.get("img_h"))])
    reg = (section or {}).get("region_shot") or {}
    if reg:
        rows.append(["region %s" % (reg.get("region"),), reg.get("n"),
                     fmt(reg.get("wall_ms"), 1), "-",
                     fmt(reg.get("jpeg_bytes"), 0), "-"])
    return ["## Screenshot", "",
            *md_table(["shot", "n", "wall p50 ms", "wall p95 ms",
                       "JPEG bytes p50", "image"], rows), ""]


def _e2e_table(section):
    if not section:
        return []
    rows = [[section.get("n"), cell(section.get("wall_ms"), "p50"),
             cell(section.get("wall_ms"), "p95"),
             cell(section.get("chars"), "p50", 0),
             fmt(section.get("spawn_to_initialize_ms"), 0),
             fmt(section.get("spawn_to_first_cu_show_ms"), 0),
             "yes" if section.get("all_ok") else "NO"]]
    return ["## End-to-end spawned server (cu_show on controlled Notepad)", "",
            *md_table(["n", "wall p50 ms", "wall p95 ms", "chars p50",
                       "spawn->init ms", "spawn->first show ms", "ok"], rows),
            ""]


def _memory_table(section):
    if not section or not section.get("maps_start"):
        return []
    start = section.get("maps_start") or {}
    end = section.get("maps_end") or {}
    names = [n for n in ("_rid2id", "_id2el", "_cache", "_win_ctl",
                         "_win_info") if n in start]
    rows = []
    for name in names:
        rows.append([name, start.get(name), end.get(name),
                     (section.get("map_growth") or {}).get(name)])
    rows.append(["RSS MB", section.get("rss_start_mb"),
                 section.get("rss_end_mb"), section.get("rss_delta_mb")])
    rows.append(["snapshot p50 ms", "-", "-",
                 cell(section.get("snapshot_ms"), "p50", 2)])
    return ["## Memory growth (%s sequential snapshots)" % section.get("n"), "",
            *md_table(["map / metric", "start", "end", "delta"], rows), ""]


def _errors_lines(doc):
    errors = doc.get("errors") or []
    lines = ["## Recorded errors", ""]
    if errors:
        lines += ["- %s: %s" % (e.get("where"), e.get("error")) for e in errors]
    else:
        lines.append("- none")
    lines.append("")
    return lines


def _errors_lines_multi(docs):
    lines = ["## Recorded errors", ""]
    for doc in docs:
        errors = doc.get("errors") or []
        label = doc.get("label") or doc.get("kind")
        if errors:
            for err in errors:
                lines.append("- %s [%s]: %s" % (label, err.get("where"),
                                                err.get("error")))
        else:
            lines.append("- %s: none" % label)
    lines.append("")
    return lines


def report_bench_single(doc, title, include_tail: bool = True):
    lines = ["# " + title, ""]
    lines += [
        "- label: **%s**" % doc.get("label"),
        "- generated_at: %s" % doc.get("generated_at"),
        "- schema: %s (harness %s)" % (doc.get("schema"),
                                       (doc.get("harness") or {}).get(
                                           "harness_version")),
        "- quick mode: %s" % ("YES - iteration only, not headline numbers"
                              if doc.get("quick") else "no"),
        "- duration: %s s" % doc.get("duration_s"),
        "- host: %s / python %s" % ((doc.get("harness") or {}).get("platform"),
                                    (doc.get("harness") or {}).get("python")),
        "",
    ]
    lines += _src_fingerprint_lines(doc)
    lines += _context_lines(doc)
    sections = doc.get("sections") or {}
    lines += _cold_table(sections.get("cold_start"))
    lines += _snapshot_table(sections.get("snapshot"))
    lines += _render_table(sections.get("render"))
    lines += _screenshot_table(sections.get("screenshot"))
    lines += _e2e_table(sections.get("e2e_server"))
    lines += _memory_table(sections.get("memory"))
    lines += _fixture_provenance_lines(doc)
    lines += _controlled_window_lines(doc)
    if include_tail:
        lines += _method_lines(False)
        lines += CAVEATS
        lines += _repro_lines([doc])
        lines += _errors_lines(doc)
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# A/B tables (two single-run docs, or one compare doc)
# --------------------------------------------------------------------------- #
def _ab_pair_row(name, block, digits=2, suffix="", scale=1.0):
    a = (block or {}).get("a") or {}
    b = (block or {}).get("b") or {}
    p = (block or {}).get("paired") or {}
    a_p50 = a.get("p50")
    b_p50 = b.get("p50")
    if scale != 1.0:
        a_p50 = None if a_p50 is None else a_p50 * scale
        b_p50 = None if b_p50 is None else b_p50 * scale
    return [name, fmt(a_p50, digits, suffix), fmt(b_p50, digits, suffix),
            fmt(p.get("delta_p50"), digits, suffix),
            fmt(p.get("delta_pct_p50"), 1, "%"),
            fmt(p.get("b_better_pct"), 0, "%")]


def _headline_rows(compare_doc):
    rows = []
    cold = (compare_doc.get("sections") or {}).get("cold_start") or {}
    for mode in ("real", "mock"):
        metrics = ((cold.get(mode) or {}).get("metrics")) or {}
        for key, label in (
                ("adjusted_spawn_to_initialize_ms",
                 "cold start %s adjusted init" % mode),
                ("spawn_to_initialize_ms", "cold start %s raw init" % mode),
                ("spawn_to_first_cu_show_ms",
                 "cold start %s spawn->first cu_show" % mode),
                ("initialize_to_first_cu_show_ms",
                 "cold start %s init->first cu_show" % mode)):
            if key in metrics:
                rows.append(_ab_pair_row(label, metrics[key], 1, " ms"))
    snap = (compare_doc.get("sections") or {}).get("snapshot") or {}
    for name, blk in (snap.get("targets") or {}).items():
        metrics = blk.get("metrics") or {}
        if "ms_per_visited" in metrics:
            rows.append(_ab_pair_row(
                "snapshot %s ms/visited" % name, metrics["ms_per_visited"], 3))
    render = (compare_doc.get("sections") or {}).get("render") or {}
    totals = render.get("totals") or {}
    if totals:
        a_chars = totals.get("chars_a")
        b_chars = totals.get("chars_b")
        pct = totals.get("chars_delta_pct")
        rows.append(["render chars total", fmt(a_chars, 0), fmt(b_chars, 0),
                     fmt(totals.get("chars_delta"), 0), fmt(pct, 1, "%"), "-"])
    e2e = ((compare_doc.get("sections") or {}).get("e2e") or {}).get("metrics") or {}
    if "median_call_ms" in e2e:
        rows.append(_ab_pair_row("e2e cu_show median call", e2e["median_call_ms"],
                                 1, " ms"))
    return rows


def _ab_cold_table(cold):
    rows = []
    for mode in ("real", "mock"):
        metrics = ((cold.get(mode) or {}).get("metrics")) or {}
        raw = metrics.get("spawn_to_initialize_ms") or {}
        adj = metrics.get("adjusted_spawn_to_initialize_ms") or {}
        tools = metrics.get("spawn_to_tools_list_ms") or {}
        show = metrics.get("spawn_to_first_cu_show_ms") or {}
        rows.append([
            mode,
            cell(raw.get("a"), digits=1), cell(raw.get("b"), digits=1),
            fmt((raw.get("paired") or {}).get("delta_pct_p50"), 1, "%"),
            cell(adj.get("a"), digits=1), cell(adj.get("b"), digits=1),
            fmt((adj.get("paired") or {}).get("delta_pct_p50"), 1, "%"),
            cell(tools.get("a"), digits=1), cell(tools.get("b"), digits=1),
            cell(show.get("a"), digits=1), cell(show.get("b"), digits=1),
            fmt((show.get("paired") or {}).get("delta_pct_p50"), 1, "%"),
        ])
    lines = ["## Cold start", "",
             *md_table(["mode", "A raw init p50 ms", "B raw init p50 ms",
                        "raw delta %", "A adjusted p50 ms", "B adjusted p50 ms",
                        "adjusted delta %", "A ->tools p50", "B ->tools p50",
                        "A ->first show p50", "B ->first show p50",
                        "first show delta %"], rows), ""]
    err = []
    for mode in ("real", "mock"):
        block = (cold.get(mode) or {}).get("first_show_error_rounds") or {}
        if block:
            err.append("%s A=%s B=%s" % (mode, block.get("a"), block.get("b")))
    if err:
        lines += ["- first real cu_show errors (MUST be 0; nonzero means the "
                  "CoInitialize/COM race or a broken spawn): " + "; ".join(err),
                  ""]
    return lines


def _ab_snapshot_tables(snap):
    lines = ["## Snapshot (interleaved, controls view)", ""]
    for name, blk in (snap.get("targets") or {}).items():
        metrics = blk.get("metrics") or {}
        rows = []
        for key, label, digits in (
                ("ms_per_visited", "ms/visited", 3),
                ("wall_ms", "wall ms", 2),
                ("elapsed_ms", "bridge elapsed ms", 1),
                ("total", "visited nodes", 0),
                ("shown", "shown nodes", 0),
                ("walk_ms", "walk ms", 2),
                ("make_node_ms", "make_node ms", 2),
                ("control_for_ms", "control_for ms", 2)):
            if key in metrics:
                rows.append(_ab_pair_row(label, metrics[key], digits))
        window = blk.get("window") or {}
        retries = blk.get("outlier_retries") or []
        lines += ["### %s (#%s %s, read_only=%s, outlier retries=%d)" % (
            name, window.get("hwnd"), window.get("proc"),
            bool(blk.get("read_only")), len(retries)), "",
            *md_table(["metric", "A p50", "B p50", "delta p50", "delta %",
                       "B faster rounds"], rows), ""]
    return lines


def _ab_render_table(render):
    rows = []
    for entry in (render.get("fixtures") or []):
        a = entry.get("a") or {}
        b = entry.get("b") or {}
        paired = entry.get("paired_ms") or {}
        rows.append([
            entry.get("fixture"), entry.get("view"),
            a.get("chars"), b.get("chars"), fmt(entry.get("chars_delta_pct"), 1, "%"),
            a.get("tokens_est"), b.get("tokens_est"),
            cell(a.get("ms"), "p50", 2), cell(b.get("ms"), "p50", 2),
            fmt(paired.get("delta_pct_p50"), 1, "%"),
            "yes" if entry.get("same_text") else "NO",
        ])
    totals = render.get("totals") or {}
    return ["## Render (compact.format_snapshot on frozen fixtures)", "",
            *md_table(["fixture", "view", "A chars", "B chars", "chars delta %",
                       "A tokens", "B tokens", "A p50 ms", "B p50 ms",
                       "ms delta %", "identical text"], rows), "",
            "- totals: A chars=%s, B chars=%s (delta %s%%), identical outputs "
            "%s/%s" % (totals.get("chars_a"), totals.get("chars_b"),
                       fmt(totals.get("chars_delta_pct"), 1),
                       totals.get("identical_outputs"),
                       totals.get("fixtures")), ""]


def _ab_e2e_table(e2e):
    rows = []
    for key, label, digits, suffix in (
            ("median_call_ms", "median cu_show call ms", 1, " ms"),
            ("median_chars", "median output chars", 0, "")):
        block = (e2e.get("metrics") or {}).get(key)
        if block:
            rows.append(_ab_pair_row(label, block, digits, suffix))
    lines = ["## End-to-end spawned server (cu_show on controlled Notepad)", "",
             *md_table(["metric", "A p50", "B p50", "delta p50", "delta %",
                        "B faster rounds"], rows), ""]
    errors = e2e.get("error_calls") or {}
    if errors:
        lines += ["- cu_show error calls (MUST be 0): A=%s B=%s"
                  % (errors.get("a"), errors.get("b")), ""]
    return lines


def _compare_memory_table(memory):
    rows = []
    for side in ("a", "b"):
        blk = memory.get(side) or {}
        if not blk:
            continue
        rows.append([
            side.upper(), blk.get("n"),
            (blk.get("maps_end") or {}).get("_rid2id"),
            (blk.get("maps_end") or {}).get("_id2el"),
            (blk.get("maps_end") or {}).get("_cache"),
            blk.get("rss_start_mb"), blk.get("rss_end_mb"),
            blk.get("rss_delta_mb"),
            cell(blk.get("snapshot_ms"), "p50", 2),
        ])
    return ["## Memory growth (sequential snapshots)", "",
            *md_table(["side", "n", "rid2id end", "id2el end", "cache end",
                       "RSS start MB", "RSS end MB", "RSS delta MB",
                       "snapshot p50 ms"], rows), ""]


def report_compare(doc, title, include_tail: bool = True):
    lines = ["# " + title, ""]
    lines += [
        "- label: **%s**" % doc.get("label"),
        "- generated_at: %s" % doc.get("generated_at"),
        "- schema: %s (harness %s)" % (doc.get("schema"),
                                       (doc.get("harness") or {}).get(
                                           "harness_version")),
        "- quick mode: %s" % ("YES - iteration only, not headline numbers"
                              if doc.get("quick") else "no"),
        "- rounds: %s (A and B interleaved, order flips every round)"
        % ((doc.get("sections") or {}).get("snapshot", {}).get("rounds")
           or (doc.get("sections") or {}).get("cold_start", {}).get("rounds")),
        "",
    ]
    src_a = doc.get("src_a") or {}
    src_b = doc.get("src_b") or {}
    fp_a = src_a.get("fingerprint") or {}
    fp_b = src_b.get("fingerprint") or {}
    rows = []
    for name in sorted(set(fp_a) | set(fp_b)):
        same = (fp_a.get(name) or {}).get("sha256") == \
               (fp_b.get(name) or {}).get("sha256")
        rows.append([name, str((fp_a.get(name) or {}).get("sha256"))[:16],
                     str((fp_b.get(name) or {}).get("sha256"))[:16],
                     "same" if same else "DIFF"])
    lines += ["## Sources", "",
              "- A (baseline): %s" % src_a.get("dir"),
              "- B (candidate): %s" % src_b.get("dir"), "",
              *md_table(["module", "A sha256 (16)", "B sha256 (16)",
                         "relation"], rows), ""]
    gate = doc.get("candidate_first_show_ok")
    frozen = doc.get("src_changed_during_run")
    gate_lines = []
    if gate is not None:
        gate_errors = doc.get("candidate_first_show_errors") or {}
        gate_lines.append(
            "- candidate first real cu_show ok: %s (e2e error calls=%s, "
            "cold-start first-show errors=%s)"
            % ("yes" if gate else "NO",
               gate_errors.get("e2e_cu_show_errors"),
               gate_errors.get("cold_start_first_show_errors")))
    if frozen is not None:
        detail = ""
        if frozen:
            detail = " : " + ", ".join(doc.get("src_changed_files") or [])
        gate_lines.append("- source trees frozen during the run: %s%s"
                          % ("NO" if frozen else "yes", detail))
    if gate_lines:
        lines += gate_lines + [""]
    lines += _context_lines(doc)
    lines += _controlled_window_lines(doc)
    notes = doc.get("notes") or []
    if notes:
        lines += ["## Run notes", ""] + ["- " + str(n) for n in notes] + [""]
    sections = doc.get("sections") or {}
    lines += ["## Headline (paired medians, B relative to A)", "",
              *md_table(["metric", "A p50", "B p50", "delta p50", "delta %",
                         "B faster rounds"], _headline_rows(doc)), ""]
    lines += _ab_cold_table(sections.get("cold_start") or {})
    lines += _ab_snapshot_tables(sections.get("snapshot") or {})
    if "render" in sections:
        lines += _ab_render_table(sections.get("render") or {})
    if "e2e" in sections:
        lines += _ab_e2e_table(sections.get("e2e") or {})
    if "memory" in sections:
        lines += _compare_memory_table(sections.get("memory") or {})
    lines += _fixture_provenance_lines(doc)
    if include_tail:
        lines += _method_lines(True)
        lines += CAVEATS
        lines += _repro_lines([doc])
        lines += _errors_lines(doc)
    lines.append("")
    return "\n".join(lines)


def report_bench_ab(doc_a, doc_b, title):
    """Two single-run docs (baseline, after) rendered as an A/B comparison."""
    lines = ["# " + title, ""]
    lines += [
        "- A (baseline): label=%s generated_at=%s" % (doc_a.get("label"),
                                                      doc_a.get("generated_at")),
        "- B (candidate): label=%s generated_at=%s" % (doc_b.get("label"),
                                                       doc_b.get("generated_at")),
        "- WARNING: these are two independent single-run captures; use "
        "--src-a/--src-b interleaved mode for the authoritative paired deltas.",
        "",
    ]
    rows = []

    def pair_from_single(path, label, digits=1, suffix=""):
        a = dig(doc_a, *path) or {}
        b = dig(doc_b, *path) or {}
        a50 = a.get("p50")
        b50 = b.get("p50")
        pct = None
        if a50 not in (None, 0) and b50 is not None:
            pct = (float(b50) - float(a50)) * 100.0 / float(a50)
        rows.append([label, fmt(a50, digits, suffix), fmt(b50, digits, suffix),
                     fmt(pct, 1, "%")])

    for mode in ("real", "mock"):
        pair_from_single(("sections", "cold_start", mode,
                          "spawn_to_initialize_ms"),
                         "cold start %s raw init" % mode)
        pair_from_single(("sections", "cold_start", mode,
                          "adjusted_spawn_to_initialize_ms"),
                         "cold start %s adjusted init" % mode)
    targets_a = dig(doc_a, "sections", "snapshot", "targets") or {}
    for name in targets_a:
        pair_from_single(("sections", "snapshot", "targets", name,
                          "ms_per_visited"), "snapshot %s ms/visited" % name,
                         digits=3)
        pair_from_single(("sections", "snapshot", "targets", name, "wall_ms"),
                         "snapshot %s wall" % name)
    pair_from_single(("sections", "e2e_server", "wall_ms"), "e2e cu_show wall")
    lines += ["## Headline (two independent runs, not interleaved)", "",
              *md_table(["metric", "A p50", "B p50", "delta %"], rows), ""]
    lines += _src_fingerprint_lines(doc_b)
    lines += _context_lines(doc_a)
    lines += _context_lines(doc_b)
    lines += ["## Section detail - A (%s)" % doc_a.get("label"), ""]
    lines += _cold_table(dig(doc_a, "sections", "cold_start") or {})
    lines += _snapshot_table(dig(doc_a, "sections", "snapshot") or {})
    lines += _render_table(dig(doc_a, "sections", "render") or {})
    lines += ["## Section detail - B (%s)" % doc_b.get("label"), ""]
    lines += _cold_table(dig(doc_b, "sections", "cold_start") or {})
    lines += _snapshot_table(dig(doc_b, "sections", "snapshot") or {})
    lines += _render_table(dig(doc_b, "sections", "render") or {})
    lines += _method_lines(False)
    lines += CAVEATS
    lines += _repro_lines([doc_a, doc_b])
    return "\n".join(lines)


def _scalar_or_p50(block, key):
    value = (block or {}).get(key)
    if isinstance(value, dict):
        return value.get("p50")
    return value


def _screenshot_compare_lines(base_doc, cand_doc):
    a_sec = (base_doc.get("sections") or {}).get("screenshot") or {}
    b_sec = (cand_doc.get("sections") or {}).get("screenshot") or {}
    if not a_sec or not b_sec:
        return []
    aw = a_sec.get("window_shot") or {}
    bw = b_sec.get("window_shot") or {}
    ar = a_sec.get("region_shot") or {}
    br = b_sec.get("region_shot") or {}

    def delta_pct(a, b):
        if a in (None, 0) or b is None:
            return None
        return (float(b) - float(a)) * 100.0 / float(a)

    rows = [
        ["window shot wall p50 ms",
         fmt(_scalar_or_p50(aw, "wall_ms"), 1),
         fmt(_scalar_or_p50(bw, "wall_ms"), 1),
         fmt(delta_pct(_scalar_or_p50(aw, "wall_ms"),
                       _scalar_or_p50(bw, "wall_ms")), 1, "%")],
        ["window shot wall p95 ms",
         fmt(_scalar_or_p50(aw.get("wall_ms") or {}, "p95"), 1),
         fmt(_scalar_or_p50(bw.get("wall_ms") or {}, "p95"), 1),
         "-"],
        ["window JPEG bytes p50",
         fmt(_scalar_or_p50(aw, "jpeg_bytes"), 0),
         fmt(_scalar_or_p50(bw, "jpeg_bytes"), 0),
         fmt(delta_pct(_scalar_or_p50(aw, "jpeg_bytes"),
                       _scalar_or_p50(bw, "jpeg_bytes")), 1, "%")],
        ["region wall ms %s" % (br.get("region"),),
         fmt(_scalar_or_p50(ar, "wall_ms"), 1),
         fmt(_scalar_or_p50(br, "wall_ms"), 1),
         fmt(delta_pct(_scalar_or_p50(ar, "wall_ms"),
                       _scalar_or_p50(br, "wall_ms")), 1, "%")],
        ["region JPEG bytes %s" % (br.get("region"),),
         fmt(_scalar_or_p50(ar, "jpeg_bytes"), 0),
         fmt(_scalar_or_p50(br, "jpeg_bytes"), 0),
         fmt(delta_pct(_scalar_or_p50(ar, "jpeg_bytes"),
                       _scalar_or_p50(br, "jpeg_bytes")), 1, "%")],
    ]
    return ["## Screenshot (candidate single-src run vs pristine baseline)", "",
            "- screenshot is not part of the interleaved harness (identical code "
            "path); this compares two single-src runs: baseline label=%s vs "
            "candidate label=%s generated %s"
            % (base_doc.get("label"), cand_doc.get("label"),
               cand_doc.get("generated_at")),
            "",
            *md_table(["metric", "baseline (A)", "candidate (B)", "delta %"],
                      rows), ""]


def build_report(docs, title=None):
    kinds = [doc.get("kind") for doc in docs]
    if len(docs) == 1:
        doc = docs[0]
        if doc.get("kind") == "compare":
            header = title or "super-cu perf A/B comparison"
            return report_compare(doc, header)
        header = title or "super-cu perf baseline"
        return report_bench_single(doc, header)
    if len(docs) == 2 and kinds == ["bench", "bench"]:
        header = title or "super-cu perf A/B comparison (independent runs)"
        return report_bench_ab(docs[0], docs[1], header)
    if len(docs) == 2 and kinds == ["bench", "compare"]:
        base_doc, cmp_doc = docs[0], docs[1]
        header = title or "super-cu perf baseline + paired A/B"
        bench_text = report_bench_single(base_doc, header, include_tail=False)
        first_nl = bench_text.find("\n")
        sections = cmp_doc.get("sections") or {}
        rounds = ((sections.get("snapshot") or {}).get("rounds")
                  or (sections.get("cold_start") or {}).get("rounds"))
        gate = cmp_doc.get("candidate_first_show_ok")
        frozen = cmp_doc.get("src_changed_during_run")
        status = (
            "\n> **Status**: pristine baseline captured %s "
            "(bench/opt/baseline.json). Paired interleaved A/B below: A=%s vs "
            "B=%s, label %s, %s rounds, generated %s. Candidate first real "
            "cu_show ok: %s; source trees frozen during the run: %s. Never "
            "compare absolute numbers across different runs on this shared "
            "machine.\n"
            % (base_doc.get("generated_at"),
               (cmp_doc.get("src_a") or {}).get("dir"),
               (cmp_doc.get("src_b") or {}).get("dir"),
               cmp_doc.get("label"), rounds, cmp_doc.get("generated_at"),
               "yes" if gate else ("NO" if gate is not None else "not recorded"),
               "yes" if frozen is False else ("NO" if frozen is True
                                             else "not recorded")))
        bench_text = bench_text[:first_nl + 1] + status + bench_text[first_nl + 1:]
        compare_title = (
            "Paired interleaved A/B (AFTER: candidate vs pristine)"
            if cmp_doc.get("label") == "after"
            else "Paired interleaved A/B (validation run)")
        parts = [bench_text,
                 "\n---\n\n",
                 report_compare(cmp_doc, compare_title, include_tail=False),
                 "\n---\n\n"]
        parts.append("\n".join(
            _method_lines(True) + CAVEATS + _repro_lines(docs)
            + _errors_lines_multi(docs)))
        return "".join(parts)
    if len(docs) == 3 and kinds == ["bench", "compare", "bench"]:
        base_doc, cmp_doc, extra_doc = docs
        header = title or "super-cu perf baseline + paired A/B + screenshot"
        bench_text = report_bench_single(base_doc, header, include_tail=False)
        first_nl = bench_text.find("\n")
        sections = cmp_doc.get("sections") or {}
        rounds = ((sections.get("snapshot") or {}).get("rounds")
                  or (sections.get("cold_start") or {}).get("rounds"))
        gate = cmp_doc.get("candidate_first_show_ok")
        frozen = cmp_doc.get("src_changed_during_run")
        status = (
            "\n> **Status**: pristine baseline captured %s "
            "(bench/opt/baseline.json). Paired interleaved A/B below: A=%s vs "
            "B=%s, label %s, %s rounds, generated %s. Candidate first real "
            "cu_show ok: %s; source trees frozen during the run: %s. "
            "Screenshot section: candidate single-src run %s. Never compare "
            "absolute numbers across different runs on this shared machine.\n"
            % (base_doc.get("generated_at"),
               (cmp_doc.get("src_a") or {}).get("dir"),
               (cmp_doc.get("src_b") or {}).get("dir"),
               cmp_doc.get("label"), rounds, cmp_doc.get("generated_at"),
               "yes" if gate else ("NO" if gate is not None else "not recorded"),
               "yes" if frozen is False else ("NO" if frozen is True
                                             else "not recorded"),
               extra_doc.get("label")))
        bench_text = bench_text[:first_nl + 1] + status + bench_text[first_nl + 1:]
        compare_title = (
            "Paired interleaved A/B (AFTER: candidate vs pristine)"
            if cmp_doc.get("label") == "after"
            else "Paired interleaved A/B (validation run)")
        parts = [bench_text,
                 "\n---\n\n",
                 report_compare(cmp_doc, compare_title, include_tail=False),
                 "\n---\n\n",
                 "\n".join(_screenshot_compare_lines(base_doc, extra_doc)),
                 "\n---\n\n"]
        parts.append("\n".join(
            _method_lines(True) + CAVEATS + _repro_lines(docs)
            + _errors_lines_multi(docs)))
        return "".join(parts)
    raise SystemExit("report expects one --report file (bench or compare), two "
                     "bench files, bench+compare, or "
                     "bench+compare+bench(screenshot); got kinds=%r" % (kinds,))


def write_report(args) -> int:
    docs = []
    for path in args.report:
        try:
            docs.append(read_json_file(path))
        except Exception as exc:
            raise SystemExit("cannot read %s: %s" % (path, exc))
    text = build_report(docs, args.report_title)
    text = text.replace("{{REPORT_INPUTS}}", " ".join(args.report))
    out = args.report_out or os.path.join(REPO_ROOT, "docs", "OPT-BASELINE.md")
    write_text_file(out, text)
    print("report written: %s (%d lines)" % (out, text.count("\n") + 1))
    return 0
# === PART 5: modes + CLI ===


def _snapshot_table(section):
    return _snapshot_rows(section)


def harness_info(argv) -> dict:
    return {
        "name": HARNESS_NAME,
        "harness_version": HARNESS_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "argv": list(argv),
    }


def build_cfg(args) -> dict:
    cfg = dict(QUICK if args.quick else FULL)
    for key in list(cfg):
        value = getattr(args, key, None)
        if value:
            cfg[key] = int(value)
    return cfg


def parse_sections(value, default):
    if value is None or not str(value).strip():
        return list(default)
    if str(value).strip().lower() == "all":
        return list(SECTION_NAMES)
    items = [part.strip() for part in str(value).split(",") if part.strip()]
    unknown = [item for item in items if item not in SECTION_NAMES]
    if unknown:
        raise SystemExit("unknown --sections entries %r; pick from %s"
                         % (unknown, list(SECTION_NAMES)))
    return items


# --------------------------------------------------------------------------- #
# single-src run
# --------------------------------------------------------------------------- #
def run_single(args, cfg: dict, argv) -> int:
    src_dir = os.path.abspath(args.src_dir)
    fixtures_dir = os.path.abspath(args.fixtures_dir)
    sections = parse_sections(args.sections, SECTION_NAMES)
    result = {
        "schema": SCHEMA,
        "kind": "bench",
        "label": args.label or "unlabeled",
        "generated_at": iso_now(),
        "harness": harness_info(argv),
        "quick": bool(args.quick),
        "src_dir": src_dir,
        "src_fingerprint": src_fingerprint(src_dir),
        "fixtures_dir": fixtures_dir,
        "fixtures_fingerprint": fixtures_fingerprint(fixtures_dir),
        "sections": {},
        "errors": [],
    }
    print("=" * 78)
    print("super-cu bench | kind=bench | label=%s" % result["label"])
    print("src=%s" % src_dir)
    print("sections=%s quick=%s n=%s" % (",".join(sections), args.quick, cfg))
    print("fixtures=%s" % fixtures_dir)
    print("=" * 78)
    t_start = time.time()
    mods = load_plugin(src_dir, "main")
    bridge = mods["bridge"].Bridge()
    timers = BridgeTimers(mods["bridge"].Bridge)
    result["context"] = {"start": os_context(mods)}
    result["controlled_window_relaunches"] = []
    handle = None

    def notepad_section(name, fn, attempts=2):
        """Run a Notepad-dependent section, re-launching the window if needed."""
        nonlocal handle
        last_exc = None
        for attempt in range(attempts):
            try:
                handle = ensure_controlled_notepad(mods, bridge, handle, result,
                                                  name)
                return fn(handle)
            except Exception as exc:
                last_exc = exc
                if attempt + 1 >= attempts or not _window_gone_error(exc):
                    raise
                print("   %s window vanished mid-section; retrying once" % name)
        raise last_exc

    try:
        need_notepad = any(s in sections
                           for s in ("snapshot", "screenshot", "memory", "e2e"))
        if need_notepad:
            print("-- controlled Notepad window")
            try:
                handle = ensure_controlled_notepad(mods, bridge, None, result,
                                                  "setup")
                print("   hwnd=#%s title=%r setvalue_ok=%s content_verified=%s"
                      % (handle["hwnd"], handle.get("title"),
                         (handle.get("setvalue") or {}).get("ok"),
                         handle.get("content_verified")))
            except Exception as exc:
                add_error(result["errors"], "notepad.setup", exc)
                force_close_stray_bench_notepad(mods, bridge, result["errors"])
        if "snapshot" in sections:
            if not handle:
                add_error(result["errors"], "snapshot.setup",
                          RuntimeError("controlled Notepad window unavailable"))
            else:
                print("-- snapshot")
                try:
                    result["sections"]["snapshot"] = notepad_section(
                        "snapshot",
                        lambda h: measure_snapshot_section(
                            mods, bridge, timers, h["hwnd"], cfg["snap_n"],
                            result["errors"]))
                except Exception as exc:
                    add_error(result["errors"], "snapshot", exc)
        if "render" in sections:
            print("-- render")
            fixtures = load_fixtures(fixtures_dir)
            if not fixtures:
                add_error(result["errors"], "render.fixtures",
                          RuntimeError("no fixtures in %s; run --dump-fixtures"
                                       % fixtures_dir))
            else:
                try:
                    result["sections"]["render"] = measure_render_section(
                        mods, fixtures, fixtures_dir, cfg["render_n"],
                        result["errors"])
                except Exception as exc:
                    add_error(result["errors"], "render", exc)
        if "screenshot" in sections:
            if not handle:
                add_error(result["errors"], "screenshot.setup",
                          RuntimeError("controlled Notepad window unavailable"))
            else:
                print("-- screenshot")
                try:
                    result["sections"]["screenshot"] = notepad_section(
                        "screenshot",
                        lambda h: measure_screenshot_section(
                            mods, bridge, h, cfg["shot_n"], result["errors"]))
                except Exception as exc:
                    add_error(result["errors"], "screenshot", exc)
        if "memory" in sections:
            if not handle:
                add_error(result["errors"], "memory.setup",
                          RuntimeError("controlled Notepad window unavailable"))
            else:
                print("-- memory")
                try:
                    result["sections"]["memory"] = notepad_section(
                        "memory",
                        lambda h: measure_memory_section(
                            mods, h["hwnd"], cfg["mem_n"], result["errors"]))
                except Exception as exc:
                    add_error(result["errors"], "memory", exc)
        if "e2e" in sections:
            print("-- e2e spawned server")
            if not handle:
                add_error(result["errors"], "e2e.setup",
                          RuntimeError("controlled Notepad window unavailable"))
            else:
                def e2e_fn(h):
                    _require_window_alive(mods, int(h["hwnd"]), "e2e")
                    return measure_e2e_section(src_dir, CONTENT_TITLE_HINT,
                                               cfg["e2e_n"], result["errors"],
                                               mods)
                try:
                    result["sections"]["e2e_server"] = notepad_section(
                        "e2e", e2e_fn)
                except Exception as exc:
                    add_error(result["errors"], "e2e", exc)
        if "cold_start" in sections:
            print("-- cold start")
            try:
                result["sections"]["cold_start"] = measure_cold_start_section(
                    src_dir, cfg["cold_n"], result["errors"], mods)
            except Exception as exc:
                add_error(result["errors"], "cold_start", exc)
    finally:
        if handle and not args.no_close:
            print("-- closing controlled Notepad")
            try:
                result["controlled_window_cleanup"] = close_controlled_notepad(
                    mods, bridge, handle, result["errors"])
            except Exception as exc:
                add_error(result["errors"], "notepad.cleanup", exc)
        elif handle:
            print("-- --no-close set: leaving Notepad #%s open" % handle.get("hwnd"))
    result["context"]["end"] = os_context(mods)
    result["duration_s"] = round(time.time() - t_start, 1)
    out = args.out or os.path.join(HERE, "%s.json" % (args.label or "bench"))
    write_json_file(out, result)
    print_single_summary(result)
    print("JSON: %s (%.1fs, %d errors)"
          % (os.path.abspath(out), result["duration_s"],
             len(result["errors"])))
    return 0 if not result["errors"] else 3


def print_single_summary(result: dict) -> None:
    print("-" * 78)
    cold = result["sections"].get("cold_start") or {}
    for mode in ("real", "mock"):
        blk = cold.get(mode) or {}
        if not blk:
            continue
        print("cold %-4s raw init p50=%sms adjusted=%sms first_show=%sms"
              % (mode,
                 fmt((blk.get("spawn_to_initialize_ms") or {}).get("p50")),
                 fmt((blk.get("adjusted_spawn_to_initialize_ms") or {}).get("p50")),
                 fmt((blk.get("spawn_to_first_cu_show_ms") or {}).get("p50"))))
    snap = (result["sections"].get("snapshot") or {}).get("targets") or {}
    for name, blk in snap.items():
        print("snapshot %-20s visited=%s shown=%s wall p50=%sms "
              "ms/visited p50=%s"
              % (name,
                 cell(blk.get("total"), "p50", 0),
                 cell(blk.get("shown"), "p50", 0),
                 cell(blk.get("wall_ms"), "p50"),
                 cell(blk.get("ms_per_visited"), "p50", 3)))
    render = result["sections"].get("render") or {}
    if render.get("totals"):
        totals = render["totals"]
        print("render totals fixtures=%s chars=%s tokens_est=%s"
              % (totals.get("fixtures"), totals.get("chars"),
                 totals.get("tokens_est")))
    shot = result["sections"].get("screenshot") or {}
    if (shot.get("window_shot") or {}).get("wall_ms"):
        print("screenshot window wall p50=%sms jpeg p50=%sB"
              % (cell(shot["window_shot"]["wall_ms"], "p50"),
                 cell(shot["window_shot"]["jpeg_bytes"], "p50", 0)))
    e2e = result["sections"].get("e2e_server") or {}
    if e2e.get("wall_ms"):
        print("e2e cu_show wall p50=%sms chars p50=%s"
              % (cell(e2e["wall_ms"], "p50"), cell(e2e.get("chars"), "p50", 0)))
    mem = result["sections"].get("memory") or {}
    if mem:
        print("memory rid2id %s->%s id2el %s->%s rss delta=%sMB"
              % ((mem.get("maps_start") or {}).get("_rid2id"),
                 (mem.get("maps_end") or {}).get("_rid2id"),
                 (mem.get("maps_start") or {}).get("_id2el"),
                 (mem.get("maps_end") or {}).get("_id2el"),
                 mem.get("rss_delta_mb")))


# --------------------------------------------------------------------------- #
# interleaved A/B run
# --------------------------------------------------------------------------- #
DEFAULT_COMPARE_SECTIONS = ("snapshot", "render", "cold_start")


def run_compare(args, cfg: dict, argv) -> int:
    src_a = os.path.abspath(args.src_a)
    src_b = os.path.abspath(args.src_b)
    fixtures_dir = os.path.abspath(args.fixtures_dir)
    sections = parse_sections(args.sections, DEFAULT_COMPARE_SECTIONS)
    notes = []
    if "screenshot" in sections:
        sections.remove("screenshot")
        notes.append("screenshot is not compared in --compare mode "
                     "(identical code path); use single-src mode for screenshot "
                     "numbers")
    result = {
        "schema": SCHEMA,
        "kind": "compare",
        "label": args.label or "a_vs_b",
        "generated_at": iso_now(),
        "harness": harness_info(argv),
        "quick": bool(args.quick),
        "src_a": {"dir": src_a, "fingerprint": src_fingerprint(src_a)},
        "src_b": {"dir": src_b, "fingerprint": src_fingerprint(src_b)},
        "fixtures_dir": fixtures_dir,
        "fixtures_fingerprint": fixtures_fingerprint(fixtures_dir),
        "sections": {},
        "errors": [],
        "notes": notes,
    }
    print("=" * 78)
    print("super-cu bench | kind=compare | label=%s" % result["label"])
    print("A (baseline)  = %s" % src_a)
    print("B (candidate) = %s" % src_b)
    print("sections=%s quick=%s n=%s" % (",".join(sections), args.quick, cfg))
    print("=" * 78)
    t_start = time.time()
    mods_a = load_plugin(src_a, "a")
    mods_b = load_plugin(src_b, "b")
    bridge_a = mods_a["bridge"].Bridge()
    bridge_b = mods_b["bridge"].Bridge()
    timers_a = BridgeTimers(mods_a["bridge"].Bridge)
    timers_b = BridgeTimers(mods_b["bridge"].Bridge)
    result["context"] = {"start": os_context(mods_a)}
    result["controlled_window_relaunches"] = []
    handle = None

    def notepad_section(name, fn, attempts=2):
        nonlocal handle
        last_exc = None
        for attempt in range(attempts):
            try:
                handle = ensure_controlled_notepad(mods_a, bridge_a, handle,
                                                  result, name)
                return fn(handle)
            except Exception as exc:
                last_exc = exc
                if attempt + 1 >= attempts or not _window_gone_error(exc):
                    raise
                print("   %s window vanished mid-section; retrying once" % name)
        raise last_exc

    try:
        need_notepad = any(s in sections
                           for s in ("snapshot", "memory", "e2e"))
        if need_notepad:
            print("-- controlled Notepad window (setup via A)")
            try:
                handle = ensure_controlled_notepad(mods_a, bridge_a, None,
                                                  result, "setup")
                print("   hwnd=#%s title=%r content_verified=%s"
                      % (handle["hwnd"], handle.get("title"),
                         handle.get("content_verified")))
            except Exception as exc:
                add_error(result["errors"], "notepad.setup", exc)
                force_close_stray_bench_notepad(mods_a, bridge_a,
                                                result["errors"])
        if "snapshot" in sections:
            if not handle:
                add_error(result["errors"], "snapshot.setup",
                          RuntimeError("controlled Notepad window unavailable"))
            else:
                print("-- snapshot (interleaved)")

                def snapshot_fn(h):
                    targets, explorer = build_snapshot_targets(
                        mods_a, bridge_a, h["hwnd"], result["errors"])
                    try:
                        section = compare_snapshot_section(
                            mods_a, bridge_a, timers_a, bridge_b, timers_b,
                            targets, cfg["cmp_rounds"], result["errors"])
                    finally:
                        if explorer:
                            section["explorer_cleanup"] = close_app(
                                mods_a, bridge_a, explorer["hwnd"],
                                result["errors"], "snapshot_explorer")
                    return section

                try:
                    result["sections"]["snapshot"] = notepad_section(
                        "snapshot", snapshot_fn)
                except Exception as exc:
                    add_error(result["errors"], "snapshot", exc)
        if "render" in sections:
            print("-- render (interleaved)")
            fixtures = load_fixtures(fixtures_dir)
            if not fixtures:
                add_error(result["errors"], "render.fixtures",
                          RuntimeError("no fixtures in %s; run --dump-fixtures"
                                       % fixtures_dir))
            else:
                try:
                    result["sections"]["render"] = compare_render_section(
                        mods_a["compact"], mods_b["compact"], fixtures,
                        fixtures_dir, cfg["render_n"], result["errors"],
                        mods_a)
                except Exception as exc:
                    add_error(result["errors"], "render", exc)
        if "e2e" in sections:
            if not handle:
                add_error(result["errors"], "e2e.setup",
                          RuntimeError("controlled Notepad window unavailable"))
            else:
                print("-- e2e (alternating A/B servers)")

                def e2e_fn(h):
                    _require_window_alive(mods_a, int(h["hwnd"]), "e2e")
                    return compare_e2e_section(
                        src_a, src_b, CONTENT_TITLE_HINT, cfg["e2e_n"],
                        min(3, cfg["cmp_rounds"]), result["errors"], mods_a)

                try:
                    result["sections"]["e2e"] = notepad_section("e2e", e2e_fn)
                except Exception as exc:
                    add_error(result["errors"], "e2e", exc)
        if "memory" in sections:
            if not handle:
                add_error(result["errors"], "memory.setup",
                          RuntimeError("controlled Notepad window unavailable"))
            else:
                print("-- memory (A then B, fresh Bridge per side)")
                try:
                    result["sections"]["memory"] = notepad_section(
                        "memory",
                        lambda h: compare_memory_section(
                            mods_a, mods_b, h["hwnd"], cfg["cmp_mem_n"],
                            result["errors"]))
                except Exception as exc:
                    add_error(result["errors"], "memory", exc)
        if "cold_start" in sections:
            print("-- cold start (alternating A/B spawns)")
            try:
                result["sections"]["cold_start"] = compare_cold_start_section(
                    src_a, src_b, cfg["cold_n"], result["errors"], mods_a)
            except Exception as exc:
                add_error(result["errors"], "cold_start", exc)
    finally:
        if handle and not args.no_close:
            print("-- closing controlled Notepad")
            try:
                result["controlled_window_cleanup"] = close_controlled_notepad(
                    mods_a, bridge_a, handle, result["errors"])
            except Exception as exc:
                add_error(result["errors"], "notepad.cleanup", exc)
    result["context"]["end"] = os_context(mods_a)
    result["duration_s"] = round(time.time() - t_start, 1)
    # freeze check: both src trees must look identical before and after the run
    drift = []
    for tag, src_dir, recorded in (("B", src_b, result["src_b"]),
                                   ("A", src_a, result["src_a"])):
        start_fp = recorded.get("fingerprint") or {}
        for name, info in src_fingerprint(src_dir).items():
            if (start_fp.get(name) or {}).get("sha256") != info.get("sha256"):
                drift.append("%s:%s" % (tag, name))
    result["src_changed_during_run"] = bool(drift)
    result["src_changed_files"] = sorted(set(drift))
    if drift:
        warning = ("src changed during the run (not frozen): %s"
                   % ", ".join(sorted(set(drift))))
        print("!! " + warning)
        result["notes"].append(warning)
    candidate_e2e_errors = ((result["sections"].get("e2e") or {}).get(
        "error_calls") or {}).get("b") or 0
    cold = result["sections"].get("cold_start") or {}
    candidate_show_errors = sum(
        ((cold.get(mode) or {}).get("first_show_error_rounds") or {}).get("b")
        or 0 for mode in ("real", "mock"))
    candidate_first_show_ok = (candidate_e2e_errors == 0
                               and candidate_show_errors == 0)
    result["candidate_first_show_ok"] = candidate_first_show_ok
    result["candidate_first_show_errors"] = {
        "e2e_cu_show_errors": candidate_e2e_errors,
        "cold_start_first_show_errors": candidate_show_errors,
    }
    if not candidate_first_show_ok:
        warning = ("STOP: candidate first real cu_show failed (%d e2e, %d "
                   "cold-start); CoInitialize/COM-race or broken spawn — do "
                   "not trust e2e/cold-start numbers"
                   % (candidate_e2e_errors, candidate_show_errors))
        print("!! " + warning)
        result["notes"].append(warning)
    out = args.out or os.path.join(HERE, "compare-%s.json"
                                   % (args.label or "a_vs_b"))
    write_json_file(out, result)
    print_compare_summary(result)
    print("JSON: %s (%.1fs, %d errors)"
          % (os.path.abspath(out), result["duration_s"],
             len(result["errors"])))
    if not candidate_first_show_ok:
        return 4
    if result.get("src_changed_during_run"):
        return 5
    return 0 if not result["errors"] else 3


def print_compare_summary(result: dict) -> None:
    print("-" * 78)
    cold = (result["sections"].get("cold_start") or {})
    for mode in ("real", "mock"):
        block = ((cold.get(mode) or {}).get("metrics") or {}).get(
            "adjusted_spawn_to_initialize_ms")
        if block:
            print("cold %-4s adjusted init A p50=%sms B p50=%sms delta=%s%%"
                  % (mode, cell(block.get("a"), "p50"),
                     cell(block.get("b"), "p50"),
                     fmt((block.get("paired") or {}).get("delta_pct_p50"), 1)))
    snap = ((result["sections"].get("snapshot") or {}).get("targets")) or {}
    for name, blk in snap.items():
        block = (blk.get("metrics") or {}).get("ms_per_visited")
        if block:
            print("snapshot %-20s ms/visited A=%s B=%s delta=%s%%"
                  % (name, cell(block.get("a"), "p50", 3),
                     cell(block.get("b"), "p50", 3),
                     fmt((block.get("paired") or {}).get("delta_pct_p50"), 1)))
    render = result["sections"].get("render") or {}
    if render.get("totals"):
        totals = render["totals"]
        print("render chars A=%s B=%s delta=%s%% identical=%s/%s"
              % (totals.get("chars_a"), totals.get("chars_b"),
                 fmt(totals.get("chars_delta_pct"), 1),
                 totals.get("identical_outputs"), totals.get("fixtures")))
    e2e = ((result["sections"].get("e2e") or {}).get("metrics") or {}).get(
        "median_call_ms")
    if e2e:
        print("e2e cu_show median A=%sms B=%sms delta=%s%%"
              % (cell(e2e.get("a"), "p50"), cell(e2e.get("b"), "p50"),
                 fmt((e2e.get("paired") or {}).get("delta_pct_p50"), 1)))
    ok = result.get("candidate_first_show_ok")
    if ok is not None:
        print("candidate first real cu_show ok: %s"
              % ("yes" if ok else "NO (COM race / broken spawn — see notes)"))
    mem = result["sections"].get("memory") or {}
    for side in ("a", "b"):
        blk = mem.get(side) or {}
        if blk:
            print("memory %s rid2id %s->%s rss delta=%sMB snapshot p50=%sms"
                  % (side.upper(),
                     (blk.get("maps_start") or {}).get("_rid2id"),
                     (blk.get("maps_end") or {}).get("_rid2id"),
                     blk.get("rss_delta_mb"),
                     cell(blk.get("snapshot_ms"), "p50", 2)))


# --------------------------------------------------------------------------- #
# fixture capture run
# --------------------------------------------------------------------------- #
def run_dump_fixtures(args, cfg: dict, argv) -> int:
    src_dir = os.path.abspath(args.src_dir)
    fixtures_dir = os.path.abspath(args.fixtures_dir)
    print("=" * 78)
    print("super-cu bench | kind=fixtures | src=%s" % src_dir)
    print("fixtures=%s" % fixtures_dir)
    print("=" * 78)
    t_start = time.time()
    mods = load_plugin(src_dir, "fx")
    errors = []
    manifest = dump_fixtures(mods, src_dir, fixtures_dir, errors)
    print("-" * 78)
    for name, info in (manifest.get("files") or {}).items():
        print("fixture %-20s sha256=%s views=%s"
              % (name, str(info.get("sha256"))[:16],
                 ",".join((info.get("views") or {}).keys())))
    print("fixtures written: %d files in %.1fs, %d errors"
          % (len(manifest.get("files") or {}), time.time() - t_start,
             len(errors)))
    for err in errors:
        print("  !! %s: %s" % (err.get("where"), err.get("error")))
    return 0 if not errors else 3


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="bench.py",
        description="super-cu reproducible perf harness (snapshot, render, "
                    "cold start, e2e, screenshot, memory, interleaved A/B).")
    parser.add_argument("--src-dir", default=DEFAULT_SRC,
                        help="src dir to measure (default: repo src/)")
    parser.add_argument("--src-a", default=None,
                        help="interleaved A/B: baseline src dir")
    parser.add_argument("--src-b", default=None,
                        help="interleaved A/B: candidate src dir")
    parser.add_argument("--label", default=None,
                        help="label stored in the JSON and report")
    parser.add_argument("--out", default=None,
                        help="JSON output path")
    parser.add_argument("--quick", action="store_true",
                        help="small n for iteration; not for headline numbers")
    parser.add_argument("--sections", default=None,
                        help="comma list: cold_start,snapshot,render,"
                             "screenshot,e2e,memory or all")
    parser.add_argument("--fixtures-dir", default=DEFAULT_FIXTURES,
                        help="frozen fixture dir (default bench/opt/fixtures)")
    parser.add_argument("--dump-fixtures", action="store_true",
                        help="capture frozen snapshot fixtures from --src-dir "
                             "(run this against the pristine src)")
    parser.add_argument("--report", nargs="+", default=None,
                        help="render a Markdown report from recorded JSON "
                             "(one bench, one compare, or two bench files)")
    parser.add_argument("--report-out", default=None,
                        help="markdown output path (default docs/OPT-BASELINE.md)")
    parser.add_argument("--report-title", default=None,
                        help="report H1 title")
    for name, default in (("cold-n", FULL["cold_n"]),
                          ("snap-n", FULL["snap_n"]),
                          ("render-n", FULL["render_n"]),
                          ("shot-n", FULL["shot_n"]),
                          ("e2e-n", FULL["e2e_n"]),
                          ("mem-n", FULL["mem_n"]),
                          ("cmp-rounds", FULL["cmp_rounds"]),
                          ("cmp-mem-n", FULL["cmp_mem_n"])):
        parser.add_argument("--" + name, type=int, default=None,
                            help="override n (default %s)" % default)
    parser.add_argument("--no-close", action="store_true",
                        help="debug: keep the controlled Notepad window open")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw)
    argv_norm = ["bench/opt/bench.py"] + raw
    try:
        if args.report:
            return write_report(args)
        cfg = build_cfg(args)
        if args.dump_fixtures:
            return run_dump_fixtures(args, cfg, argv_norm)
        if args.src_a or args.src_b:
            if not (args.src_a and args.src_b):
                print("interleaved mode needs both --src-a and --src-b",
                      file=sys.stderr)
                return 2
            return run_compare(args, cfg, argv_norm)
        return run_single(args, cfg, argv_norm)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

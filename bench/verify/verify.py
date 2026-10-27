#!/usr/bin/env python3
"""Independent verifier for the super-cu optimization project (verifier-owned).

Two pristine/modified plugin trees are compared from *separate* interpreter
processes (see _vcap.py) so equal module names never collide:

    python -X utf8 bench/verify/verify.py run
    python -X utf8 bench/verify/verify.py run --real --hwnds 123,456
    python -X utf8 bench/verify/verify.py protocol --src-dir <plugin>/src --mock
    python -X utf8 bench/verify/verify.py compare --base a.json --cur b.json

Hard gates: snapshot/bridge parity, render golden identity (or budget+recall
invariants if a format change was approved), id stability, fallback safety,
memory bounds, screenshot/DPI invariants, protocol purity/error codes, tool
schema identity.  Soft (WARN) gates: mock-scene fixture drift when mock_bridge.py
itself changed, and model-facing text differences caused by an approved compact
tuning pass.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _vshared as V  # noqa: E402

DEFAULT_ORIG = r"D:\opencode\_supercu_opt\orig\src"
DEFAULT_SRC = r"D:\opencode\plugins\super-cu\src"
OUT_DIR = os.path.join(HERE, "out")


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _check(checks: list, name: str, ok: bool, detail: str = "",
           evidence=None, status: str | None = None) -> None:
    checks.append({"name": name,
                   "status": status or ("PASS" if ok else "FAIL"),
                   "detail": detail, "evidence": evidence})


def _skip(checks: list, name: str, detail: str, evidence=None) -> None:
    checks.append({"name": name, "status": "SKIP", "detail": detail,
                   "evidence": evidence})


def _warn(checks: list, name: str, detail: str, evidence=None) -> None:
    checks.append({"name": name, "status": "WARN", "detail": detail,
                   "evidence": evidence})


def _cpu() -> dict:
    info = {"cpu_count": os.cpu_count()}
    try:
        import psutil
        info["cpu_percent"] = psutil.cpu_percent(interval=0.2)
    except Exception:
        pass
    return info


# --------------------------------------------------------------------------- #
# child capture process (isolated interpreter per side)
# --------------------------------------------------------------------------- #
def run_capture(mode: str, src_dir: str, out_path: str, *, fixtures=None,
                hwnds="", iters=0, churn=0, extra_env=None, label="",
                views="", stale=False, dialog=False) -> dict:
    cmd = [sys.executable, "-X", "utf8", os.path.join(HERE, "_vcap.py"), mode,
           "--src-dir", os.path.abspath(src_dir), "--out", out_path]
    if fixtures:
        cmd += ["--fixtures", fixtures]
    if hwnds:
        cmd += ["--hwnds", hwnds]
    if views:
        cmd += ["--views", views]
    if iters:
        cmd += ["--iters", str(iters)]
    if churn:
        cmd += ["--churn", str(churn)]
    if stale:
        cmd += ["--stale"]
    if dialog:
        cmd += ["--dialog"]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=900, env=env,
                              cwd=os.path.dirname(HERE))
        duration = round(time.perf_counter() - started, 2)
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0:
            print("  [capture %s %s] exit=%d" % (mode, label, proc.returncode))
            print(stderr[-2000:])
    except subprocess.TimeoutExpired as exc:
        duration = round(time.perf_counter() - started, 2)
        return {"fatal": "capture timeout after %ss" % duration,
                "meta": {"src_dir": src_dir, "duration_s": duration}}
    try:
        data = V.read_json(out_path)
    except Exception as exc:
        return {"fatal": "cannot read capture: %s: %s" % (type(exc).__name__, exc),
                "meta": {"src_dir": src_dir, "duration_s": duration},
                "capture_stdout": stdout[-3000:], "capture_stderr": stderr[-3000:]}
    data.setdefault("meta", {})["capture_duration_s"] = duration
    data["meta"]["capture_stdout_tail"] = stdout[-1500:]
    data["meta"]["capture_stderr_tail"] = stderr[-1500:]
    return data


# --------------------------------------------------------------------------- #
# JSON-RPC session (protocol purity / error codes)
# --------------------------------------------------------------------------- #
class Session:
    """Talks newline-delimited JSON-RPC to a freshly spawned server process."""

    def __init__(self, server_path: str, args=(), env_extra=None,
                 timeout_s: float = 90.0):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        for key in ("SUPER_CU_MOCK", "SUPER_CU_NO_CACHE"):
            env.pop(key, None)
        if env_extra:
            env.update(env_extra)
        self.server_path = server_path
        self.started = time.perf_counter()
        self.proc = subprocess.Popen(
            [sys.executable, "-u", server_path, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=os.path.dirname(server_path))
        self.stdout_raw: list = []
        self.stderr_raw: list = []
        self.parse_errors: list = []
        self.unsolicited: list = []
        self._q: queue.Queue = queue.Queue()
        self._eq: queue.Queue = queue.Queue()
        self._threads = [
            threading.Thread(target=self._reader, args=(self.proc.stdout, self._q,
                                                        self.stdout_raw), daemon=True),
            threading.Thread(target=self._reader, args=(self.proc.stderr, self._eq,
                                                        self.stderr_raw), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self.timeout_s = timeout_s

    @staticmethod
    def _reader(stream, q, sink) -> None:
        try:
            for line in iter(stream.readline, b""):
                sink.append(line)
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)

    # -- io ---------------------------------------------------------------- #
    def send_raw(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def send(self, method: str, params=None, mid=None) -> None:
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg, ensure_ascii=False))

    def notify(self, method: str, params=None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg, ensure_ascii=False))

    def _next(self, timeout: float):
        try:
            line = self._q.get(timeout=max(0.05, timeout))
        except queue.Empty:
            raise TimeoutError("no stdout line within %.1fs" % timeout)
        if line is None:
            raise EOFError("server closed stdout")
        try:
            return json.loads(line.decode("utf-8", "replace"))
        except Exception:
            self.parse_errors.append(line.decode("utf-8", "replace")[:200])
            return None

    def wait_response(self, mid, timeout: float | None = None):
        deadline = time.time() + (timeout if timeout is not None else self.timeout_s)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("no response for id=%r" % (mid,))
            obj = self._next(remaining)
            if obj is None:
                continue
            if isinstance(obj, dict) and obj.get("id") == mid and (
                    "result" in obj or "error" in obj):
                return obj
            self.unsolicited.append(obj)

    def request(self, method: str, params=None, mid=None, timeout=None):
        self.send(method, params, mid=mid)
        return self.wait_response(mid, timeout)

    def next_message(self, timeout: float = 5.0):
        return self._next(timeout)

    # -- teardown ---------------------------------------------------------- #
    def shutdown_gracefully(self, timeout: float = 10.0) -> dict:
        result = {"exit_code": None, "graceful": False}
        try:
            self.send("shutdown", mid="shutdown-1")
            result["shutdown_response"] = self.wait_response("shutdown-1",
                                                             timeout=timeout)
        except Exception as exc:
            result["shutdown_error"] = "%s: %s" % (type(exc).__name__, exc)
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            result["exit_code"] = self.proc.wait(timeout=timeout)
            result["graceful"] = result["exit_code"] == 0
        except subprocess.TimeoutExpired:
            self.proc.kill()
            result["exit_code"] = "killed"
        try:
            self.proc.stdout.close()
            self.proc.stderr.close()
        except Exception:
            pass
        result["stdout_lines"] = len(self.stdout_raw)
        result["stderr_lines"] = len(self.stderr_raw)
        return result

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


def _tool_text(response) -> tuple:
    """(text, is_error, content_types) from a tools/call response."""
    if not isinstance(response, dict) or "result" not in response:
        return "", None, []
    result = response["result"] or {}
    blocks = result.get("content") or []
    types = [block.get("type") for block in blocks if isinstance(block, dict)]
    texts = [block.get("text") or "" for block in blocks
             if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(texts), result.get("isError"), types


def protocol_mock(server_path: str, label: str) -> dict:
    checks: list = []
    session = Session(server_path, ["--mock"])
    data: dict = {"mode": "mock", "label": label, "checks": checks,
                  "server": server_path, "texts": {}, "codes": {},
                  "timings": {}}
    started = time.perf_counter()
    try:
        init = session.request("initialize", {"protocolVersion": "2024-11-05"},
                               mid=1)
        data["timings"]["spawn_to_initialize_ms"] = int(
            (time.perf_counter() - started) * 1000)
        result = init.get("result") or {}
        _check(checks, "proto.initialize.echo",
               result.get("protocolVersion") == "2024-11-05",
               repr(result.get("protocolVersion")))
        _check(checks, "proto.initialize.capabilities",
               result.get("capabilities") == {"tools": {}},
               repr(result.get("capabilities")))
        info = result.get("serverInfo") or {}
        _check(checks, "proto.initialize.serverInfo",
               info.get("name") == "supercu" and isinstance(info.get("version"), str),
               repr(info))
        for index, params in enumerate(({}, {"protocolVersion": "2025-06-18"},
                                        {"protocolVersion": ""},
                                        {"protocolVersion": 7})):
            response = session.request("initialize", params, mid="i-%d" % index)
            got = (response.get("result") or {}).get("protocolVersion")
            want = params.get("protocolVersion") or "2024-11-05"
            if not isinstance(want, str):
                want = "2024-11-05"
            _check(checks, "proto.initialize.echo2[%r]" % (want,), got == want,
                   "got %r want %r" % (got, want))
        # notifications must never be answered
        session.notify("notifications/initialized")
        session.notify("notifications/cancelled", {"requestId": 1})
        pong = session.request("ping", mid="ping-1")
        _check(checks, "proto.notifications.silent",
               pong.get("result") == {} and not session.unsolicited,
               "unsolicited=%r" % (session.unsolicited[:2],))
        tools = session.request("tools/list", mid="tools-1")
        tool_list = (tools.get("result") or {}).get("tools") or []
        names = [tool.get("name") for tool in tool_list]
        _check(checks, "proto.tools_list.names", names == ["cu_show", "cu_do"],
               repr(names))
        data["tools"] = tool_list
        schemas_ok = True
        schema_notes = []
        for tool in tool_list:
            schema = tool.get("inputSchema") or {}
            props = schema.get("properties") or {}
            if not (isinstance(tool.get("description"), str) and tool["description"]
                    and schema.get("type") == "object" and props):
                schemas_ok = False
                schema_notes.append("bad schema for %r" % (tool.get("name"),))
                continue
            if tool.get("name") == "cu_show":
                want = {"window", "scope", "view", "filter", "max_nodes",
                        "screenshot", "region", "max_image_width"}
                missing = want - set(props)
                if missing:
                    schemas_ok = False
                    schema_notes.append("cu_show missing %s" % sorted(missing))
                if (props.get("view") or {}).get("enum") != ["controls", "text", "all"]:
                    schemas_ok = False
                    schema_notes.append("cu_show view enum changed")
            if tool.get("name") == "cu_do":
                steps = props.get("steps") or {}
                items = steps.get("items") or {}
                if steps.get("minItems") != 1 or not (items.get("properties") or {}):
                    schemas_ok = False
                    schema_notes.append("cu_do steps schema changed")
                if (props.get("observe") or {}).get("enum") != ["auto", "none", "state"]:
                    schemas_ok = False
                    schema_notes.append("cu_do observe enum changed")
        _check(checks, "proto.tools_list.schema", schemas_ok,
               "; ".join(schema_notes) if schema_notes else "ok")
        cases = [
            ("cu_show.default", "cu_show", {}),
            ("cu_show.desktop", "cu_show", {"scope": "desktop"}),
            ("cu_show.text", "cu_show", {"view": "text"}),
            ("cu_show.all", "cu_show", {"view": "all"}),
            ("cu_show.filter_hit", "cu_show", {"filter": "题目"}),
            ("cu_show.filter_miss", "cu_show", {"filter": "zzz-none"}),
            ("cu_show.window_notepad", "cu_show", {"window": "#1001"}),
            ("cu_show.max_nodes", "cu_show", {"max_nodes": 5}),
        ]
        budget_exempt = ("cu_show.desktop", "cu_show.text", "cu_show.all",
                         "cu_show.filter_hit", "cu_show.filter_miss")
        for name, tool, arguments in cases:
            try:
                response = session.request("tools/call",
                                           {"name": tool, "arguments": arguments},
                                           mid=name)
                text, is_error, types = _tool_text(response)
                data["texts"][name] = text
                _check(checks, "proto.%s.shape" % name,
                       not is_error and types == ["text"],
                       "isError=%r types=%r" % (is_error, types))
                if text:
                    _check(checks, "proto.%s.cap" % name, len(text) <= V.HARD_CAP,
                           "chars=%d" % len(text))
                if name not in budget_exempt:
                    _check(checks, "proto.%s.controls_budget" % name,
                           len(text) <= V.CONTROL_TARGET, "chars=%d" % len(text))
            except Exception as exc:
                _check(checks, "proto.%s" % name, False,
                       "%s: %s" % (type(exc).__name__, exc))
        # screenshot content block
        try:
            response = session.request("tools/call", {
                "name": "cu_show",
                "arguments": {"window": "#1001", "screenshot": True}},
                mid="shot")
            result = response.get("result") or {}
            blocks = result.get("content") or []
            types = [block.get("type") for block in blocks]
            image = next((block for block in blocks
                          if block.get("type") == "image"), None)
            ok = (types[:1] == ["text"] and image is not None
                  and image.get("mimeType") == "image/jpeg"
                  and len(image.get("data") or "") > 100)
            _check(checks, "proto.cu_show.screenshot", ok, "types=%r" % (types,),
                   {"data_len": len((image or {}).get("data") or "")})
            data["texts"]["cu_show.screenshot"] = (blocks[0].get("text") or "")
        except Exception as exc:
            _check(checks, "proto.cu_show.screenshot", False,
                   "%s: %s" % (type(exc).__name__, exc))
        try:
            response = session.request("tools/call", {
                "name": "cu_show",
                "arguments": {"window": "#1001", "screenshot": True,
                              "region": [120, 100, 320, 240],
                              "max_image_width": 640}}, mid="shot-region")
            _check(checks, "proto.cu_show.region", not _tool_text(response)[1],
                   repr(response.get("error")))
        except Exception as exc:
            _check(checks, "proto.cu_show.region", False,
                   "%s: %s" % (type(exc).__name__, exc))
        # missing window is a tool-level error with actionable advice
        try:
            response = session.request("tools/call", {
                "name": "cu_show",
                "arguments": {"window": "no-such-win-xyz"}},
                mid="cu_show.missing_window")
            text, is_error, types = _tool_text(response)
            data["texts"]["cu_show.missing_window"] = text
            _check(checks, "proto.cu_show.missing_window", is_error is True
                   and types == ["text"] and ("\u2192" in text),
                   "isError=%r text=%r" % (is_error, text[:160]))
        except Exception as exc:
            _check(checks, "proto.cu_show.missing_window", False,
                   "%s: %s" % (type(exc).__name__, exc))
        # cu_do shapes
        try:
            response = session.request("tools/call", {
                "name": "cu_do", "arguments": {"steps": [
                    {"op": "click", "target": "#999999"}]}}, mid="do-bad-id")
            text, is_error, types = _tool_text(response)
            data["texts"]["cu_do.bad_id"] = text
            _check(checks, "proto.cu_do.bad_id.is_error", is_error is True,
                   "isError=%r" % (is_error,))
            _check(checks, "proto.cu_do.bad_id.fail_line",
                   ("FAIL" in text) and ("\u2192" in text), text[:200])
        except Exception as exc:
            _check(checks, "proto.cu_do.bad_id", False,
                   "%s: %s" % (type(exc).__name__, exc))
        # wait for text that exists in the notepad mock scene (last hwnd #1001)
        try:
            response = session.request("tools/call", {
                "name": "cu_do", "arguments": {"steps": [
                    {"op": "wait", "text": "文件", "timeout_ms": 300}]}},
                mid="do-wait")
            text, is_error, types = _tool_text(response)
            data["texts"]["cu_do.wait_hit"] = text
            _check(checks, "proto.cu_do.wait_hit", not is_error, text[:160])
        except Exception as exc:
            _check(checks, "proto.cu_do.wait_hit", False,
                   "%s: %s" % (type(exc).__name__, exc))
        # a genuinely missing text must fail fast with advice (not hang/crash)
        try:
            response = session.request("tools/call", {
                "name": "cu_do", "arguments": {"steps": [
                    {"op": "wait", "text": "zzz-no-such-text",
                     "timeout_ms": 200}]}}, mid="do-wait-miss")
            text, is_error, types = _tool_text(response)
            data["texts"]["cu_do.wait_miss"] = text
            _check(checks, "proto.cu_do.wait_miss",
                   is_error is True and "FAIL" in text and "\u2192" in text,
                   text[:160])
        except Exception as exc:
            _check(checks, "proto.cu_do.wait_miss", False,
                   "%s: %s" % (type(exc).__name__, exc))
        # protocol errors
        error_cases = [
            ("err.empty_steps", "tools/call", {"name": "cu_do",
                                               "arguments": {"steps": []}}, -32602),
            ("err.bad_op", "tools/call", {"name": "cu_do", "arguments": {
                "steps": [{"op": "bogus"}]}}, -32602),
            ("err.steps_not_list", "tools/call", {"name": "cu_do",
                                                  "arguments": {"steps": 5}}, -32602),
            ("err.unknown_tool", "tools/call", {"name": "nope", "arguments": {}},
             -32602),
            ("err.arguments_not_object", "tools/call", {"name": "cu_show",
                                                        "arguments": 5}, -32602),
            ("err.missing_name", "tools/call", {"arguments": {}}, -32602),
            ("err.unknown_method", "foo/bar", {}, -32601),
            ("err.bad_view", "tools/call", {"name": "cu_show", "arguments": {
                "view": "everything"}}, -32602),
            ("err.bad_region", "tools/call", {"name": "cu_show", "arguments": {
                "region": [1, 2, 3]}}, -32602),
            ("err.bad_scope", "tools/call", {"name": "cu_show", "arguments": {
                "scope": "screen"}}, -32602),
            ("err.bad_max_nodes", "tools/call", {"name": "cu_show", "arguments": {
                "max_nodes": "many"}}, -32602),
        ]
        for name, method, params, want_code in error_cases:
            try:
                response = session.request(method, params, mid=name)
                error = response.get("error") or {}
                data["codes"][name] = error.get("code")
                _check(checks, "proto.%s.code" % name,
                       error.get("code") == want_code,
                       "code=%r want=%r msg=%r"
                       % (error.get("code"), want_code, error.get("message")))
                _check(checks, "proto.%s.message" % name,
                       isinstance(error.get("message"), str)
                       and len(error["message"]) > 3,
                       repr(error.get("message"))[:160])
            except Exception as exc:
                _check(checks, "proto.%s" % name, False,
                       "%s: %s" % (type(exc).__name__, exc))
        try:
            response = session.request("ping", 5, mid="params-not-object")
            error = response.get("error") or {}
            _check(checks, "proto.params_not_object",
                   error.get("code") == -32602,
                   "code=%r" % (error.get("code"),))
        except Exception as exc:
            _check(checks, "proto.params_not_object", False,
                   "%s: %s" % (type(exc).__name__, exc))
        # malformed JSON line -> -32700 id null, process survives
        try:
            session.send_raw("{not json")
            message = session.next_message(timeout=10)
            _check(checks, "proto.malformed_json",
                   isinstance(message, dict) and message.get("id") is None
                   and (message.get("error") or {}).get("code") == -32700,
                   repr(message)[:200])
        except Exception as exc:
            _check(checks, "proto.malformed_json", False,
                   "%s: %s" % (type(exc).__name__, exc))
        try:
            pong2 = session.request("ping", mid="ping-after-error")
            _check(checks, "proto.alive_after_malformed",
                   pong2.get("result") == {}, repr(pong2)[:160])
        except Exception as exc:
            _check(checks, "proto.alive_after_malformed", False,
                   "%s: %s" % (type(exc).__name__, exc))
        try:
            session.send_raw("[]")
            message = session.next_message(timeout=10)
            _check(checks, "proto.invalid_request",
                   isinstance(message, dict)
                   and (message.get("error") or {}).get("code") == -32600,
                   repr(message)[:160])
        except Exception as exc:
            _check(checks, "proto.invalid_request", False,
                   "%s: %s" % (type(exc).__name__, exc))
        data["timings"]["spawn_to_tools_list_ms"] = int(
            (time.perf_counter() - started) * 1000)
    except Exception as exc:
        _check(checks, "proto.fatal", False,
               "%s: %s" % (type(exc).__name__, exc),
               {"traceback": traceback.format_exc()[-1500:]})
    finally:
        teardown = session.shutdown_gracefully()
        data["teardown"] = teardown
        _check(checks, "proto.graceful_exit",
               teardown.get("graceful") is True,
               repr(teardown.get("exit_code")))
        raw = [line.decode("utf-8", "replace") for line in session.stdout_raw]
        data["stdout_line_count"] = len(raw)
        data["stdout_non_json"] = session.parse_errors + [
            line for line in raw if line.strip() and not _is_json(line)]
        _check(checks, "proto.stdout_json_only", not data["stdout_non_json"],
               "%d lines, %d bad: %r" % (len(raw), len(data["stdout_non_json"]),
                                         data["stdout_non_json"][:3]))
        stderr_text = "".join(
            line.decode("utf-8", "replace") for line in session.stderr_raw)
        data["stderr_tail"] = stderr_text[-2000:]
        prefix_ok = all(line.startswith("[supercu]")
                        for line in stderr_text.splitlines() if line.strip())
        _check(checks, "proto.stderr_prefixed", prefix_ok,
               "non-prefixed stderr line(s)" if not prefix_ok else "ok")
        data["checks"] = checks
    return data


def protocol_real(server_path: str, hwnds: list, label: str, *,
                  prefetch_wait_ms: int = 0, env_extra=None) -> dict:
    checks: list = []
    data: dict = {"mode": "real", "label": label, "checks": checks,
                  "texts": {}, "codes": {}, "timings": {},
                  "env": dict(env_extra or {}),
                  "prefetch_wait_ms": int(prefetch_wait_ms)}
    session = Session(server_path, [], env_extra=env_extra)
    started = time.perf_counter()
    try:
        init = session.request("initialize", {"protocolVersion": "2024-11-05"},
                               mid="init")
        data["timings"]["spawn_to_initialize_ms"] = int(
            (time.perf_counter() - started) * 1000)
        result = init.get("result") or {}
        _check(checks, "proto_real.initialize",
               result.get("protocolVersion") == "2024-11-05",
               repr(result.get("protocolVersion")))
        tools = session.request("tools/list", mid="tools")
        data["timings"]["spawn_to_tools_list_ms"] = int(
            (time.perf_counter() - started) * 1000)
        names = [t.get("name") for t in (tools.get("result") or {}).get("tools") or []]
        _check(checks, "proto_real.tools_list", names == ["cu_show", "cu_do"],
               repr(names))
        data["tools"] = (tools.get("result") or {}).get("tools")
        # SHIP-BLOCKER regression: after tools/list the server may have prefetched
        # the bridge module in a WORKER thread (comtypes -> CoInitializeEx there).
        # If the wait is long enough for the prefetch to finish, the first real
        # tool call must still work on the main thread (COM must be initialised
        # by the server itself, not as a side effect of a racing import).
        if prefetch_wait_ms and hwnds:
            time.sleep(prefetch_wait_ms / 1000.0)
            wait_started = time.perf_counter()
            response = session.request("tools/call", {
                "name": "cu_show",
                "arguments": {"window": "#%d" % hwnds[0], "view": "controls"}},
                mid="first-show-after-wait", timeout=120)
            text, is_error, types = _tool_text(response)
            wait_ms = int((time.perf_counter() - wait_started) * 1000)
            data["timings"]["first_show_after_wait_ms"] = wait_ms
            data["first_show"] = {"isError": is_error,
                                  "text": text[:400],
                                  "types": types}
            _check(checks, "proto_real.first_show_after_prefetch",
                   not is_error and types == ["text"],
                   "isError=%r text=%r" % (is_error, text[:240]))
        first_tool_started = time.perf_counter()
        for hwnd in hwnds or []:
            for view in ("controls", "all", "text"):
                name = "cu_show.real.%s.%s" % (hwnd, view)
                response = session.request("tools/call", {
                    "name": "cu_show",
                    "arguments": {"window": "#%d" % hwnd, "view": view}},
                    mid=name, timeout=120)
                text, is_error, types = _tool_text(response)
                data["texts"][name] = text
                _check(checks, "proto_real.%s.shape" % name,
                       not is_error and types == ["text"],
                       "isError=%r" % (is_error,))
                _check(checks, "proto_real.%s.cap" % name,
                       len(text) <= V.HARD_CAP, "chars=%d" % len(text))
                if view == "controls":
                    _check(checks, "proto_real.%s.budget" % name,
                           len(text) <= V.CONTROL_TARGET, "chars=%d" % len(text))
            # filter mode end-to-end (read-only)
            filter_response = session.request("tools/call", {
                "name": "cu_show",
                "arguments": {"window": "#%d" % hwnd, "filter": "a"}},
                mid="filter-%s" % hwnd, timeout=120)
            filter_text, filter_error, _ = _tool_text(filter_response)
            data["texts"]["cu_show.real.%s.filter" % hwnd] = filter_text
            _check(checks, "proto_real.filter.%s" % hwnd,
                   not filter_error and ("filter=" in filter_text),
                   filter_text.splitlines()[1][:120] if filter_text else "")
        data["timings"]["first_tool_call_ms"] = int(
            (time.perf_counter() - first_tool_started) * 1000)
        checks.append({
            "name": "proto_real.lazy_startup_info", "status": "INFO",
            "detail": "spawn->initialize=%sms spawn->tools/list=%sms "
                      "first-tool-call=%sms (load-dependent; the gap shows "
                      "whether the bridge import was deferred)"
                      % (data["timings"].get("spawn_to_initialize_ms"),
                         data["timings"].get("spawn_to_tools_list_ms"),
                         data["timings"].get("first_tool_call_ms")),
            "evidence": data["timings"]})
        desktop = session.request("tools/call", {
            "name": "cu_show", "arguments": {"scope": "desktop"}}, mid="desktop")
        _check(checks, "proto_real.desktop",
               "desktop" in _tool_text(desktop)[0],
               _tool_text(desktop)[0][:120])
        response = session.request("tools/call", {
            "name": "nope", "arguments": {}}, mid="unknown-tool")
        error = response.get("error") or {}
        _check(checks, "proto_real.unknown_tool_code", error.get("code") == -32602,
               repr(error))
        response = session.request("tools/call", {
            "name": "cu_show", "arguments": {"window": "no-such-window-xyz"}},
            mid="missing-window")
        text, is_error, _types = _tool_text(response)
        data["texts"]["missing_window"] = text
        _check(checks, "proto_real.missing_window", is_error is True, text[:200])
    except Exception as exc:
        _check(checks, "proto_real.fatal", False,
               "%s: %s" % (type(exc).__name__, exc),
               {"traceback": traceback.format_exc()[-1500:]})
    finally:
        teardown = session.shutdown_gracefully()
        data["teardown"] = teardown
        _check(checks, "proto_real.graceful_exit",
               teardown.get("graceful") is True, repr(teardown.get("exit_code")))
        raw = [line.decode("utf-8", "replace") for line in session.stdout_raw]
        data["stdout_non_json"] = session.parse_errors + [
            line for line in raw if line.strip() and not _is_json(line)]
        _check(checks, "proto_real.stdout_json_only", not data["stdout_non_json"],
               "%d lines, %d bad" % (len(raw), len(data["stdout_non_json"])))
        data["stderr_tail"] = "".join(
            line.decode("utf-8", "replace") for line in session.stderr_raw)[-3000:]
        data["checks"] = checks
    return data


# --------------------------------------------------------------------------- #
# A/B comparison
# --------------------------------------------------------------------------- #
def _inventory_ab(base: dict, cur: dict) -> list:
    checks: list = []
    for module in ("bridge", "windows", "compact", "server", "mock_bridge"):
        base_info = base.get(module) or {}
        cur_info = cur.get(module) or {}
        if not base_info.get("exists") or not cur_info.get("exists"):
            continue
        def public(names):
            return {name for name in names if not name.startswith("_")}

        removed = sorted(public(base_info.get("functions") or [])
                         - public(cur_info.get("functions") or []))
        removed += sorted(public((base_info.get("classes") or {}).keys())
                          - public((cur_info.get("classes") or {}).keys()))
        for cls in public((base_info.get("classes") or {}).keys()) & public(
                (cur_info.get("classes") or {}).keys()):
            removed += ["%s.%s" % (cls, name)
                        for name in sorted(
                            public(base_info["classes"][cls])
                            - public(cur_info["classes"][cls]))]
        _check(checks, "ab.api.%s.public_names" % module, not removed,
               "removed: %s" % removed[:12] if removed else "no removals")
        added = sorted(set(cur_info.get("functions") or [])
                       - set(base_info.get("functions") or []))
        if added:
            checks.append({"name": "ab.api.%s.additions" % module,
                           "status": "INFO",
                           "detail": "new functions: %s" % added[:12],
                           "evidence": None})
        base_consts = base_info.get("constants") or {}
        cur_consts = cur_info.get("constants") or {}
        changed = {key: [base_consts[key], cur_consts[key]]
                   for key in set(base_consts) & set(cur_consts)
                   if base_consts[key] != cur_consts[key]
                   and not isinstance(base_consts[key], (dict, list))}
        if changed:
            checks.append({"name": "ab.api.%s.constants_changed" % module,
                           "status": "INFO",
                           "detail": "constants changed: %s"
                                     % {k: v for k, v in list(changed.items())[:8]},
                           "evidence": None})
    return checks


def _focus_only_diffs(diffs: list) -> bool:
    """True when every divergence is the volatile 'focused' window state.

    The two A/B processes snapshot at different moments; whether the target
    window happens to be foreground is environment state, not cache behaviour.
    """
    return bool(diffs) and all("focused" in str(item) for item in diffs)


def _first_text_divergence(base_text: str, cur_text: str) -> dict:
    base_lines = base_text.splitlines()
    cur_lines = cur_text.splitlines()
    for index in range(max(len(base_lines), len(cur_lines))):
        a = base_lines[index] if index < len(base_lines) else "<missing>"
        b = cur_lines[index] if index < len(cur_lines) else "<missing>"
        if a != b:
            return {"line": index + 1, "base": a[:160], "cur": b[:160]}
    return {}


def compare_fmt(base: dict, cur: dict, *, allow_format_drift=False) -> list:
    checks: list = []
    base_renders = base.get("renders") or {}
    cur_renders = cur.get("renders") or {}

    base_mock = base.get("mock") or {}
    cur_mock = cur.get("mock") or {}
    mock_changed = ((base.get("meta") or {}).get("module_sha256") or {}).get(
        "mock_bridge") != ((cur.get("meta") or {}).get("module_sha256") or {}).get(
        "mock_bridge")
    diffs_all = {}
    for key in sorted(set(base_mock) | set(cur_mock)):
        diffs = V.compare_snapshots(base_mock.get(key), cur_mock.get(key))
        if diffs:
            diffs_all[key] = diffs
    if mock_changed:
        _warn(checks, "ab.mock_snapshot_parity",
              "mock_bridge.py changed -> mock scenes are a test scaffold, not an "
              "A/B parity target (%d scenarios differ)" % len(diffs_all),
              {"sample": list(diffs_all.items())[:2]})
    else:
        _check(checks, "ab.mock_snapshot_parity", not diffs_all,
               "%d/%d scenarios differ: %s"
               % (len(diffs_all), len(base_mock),
                  {k: v[:2] for k, v in list(diffs_all.items())[:2]})
               if diffs_all else "all %d scenarios identical" % len(base_mock))

    fixture_keys = [key for key in set(base_renders) | set(cur_renders)
                    if key.startswith("fixture|")]
    drift = []
    for key in sorted(fixture_keys):
        base_rec = base_renders.get(key) or {}
        cur_rec = cur_renders.get(key) or {}
        base_text = V.normalize_text(base_rec.get("text") or "")
        cur_text = V.normalize_text(cur_rec.get("text") or "")
        base_stats = base_rec.get("stats") or {}
        cur_stats = cur_rec.get("stats") or {}
        if base_text != cur_text:
            drift.append((key, "text", _first_text_divergence(base_text, cur_text)))
        if base_stats != cur_stats:
            drift.append((key, "stats",
                          {"base": base_stats, "cur": cur_stats}))
    if allow_format_drift:
        _warn(checks, "ab.render_golden",
              "format drift accepted: %d/%d fixtures"
              % (len(drift), len(fixture_keys)), {"first": drift[:2]})
    else:
        _check(checks, "ab.render_golden", not drift,
               "%d/%d fixture renders differ: %s"
               % (len(drift), len(fixture_keys), drift[:2])
               if drift else
               "all %d fixture renders identical" % len(fixture_keys),
               {"fixtures": len(fixture_keys), "diff_count": len(drift)})

    mock_render_keys = [key for key in set(base_renders) | set(cur_renders)
                        if key.startswith("mock|")]
    drift2 = []
    for key in sorted(mock_render_keys):
        base_text = V.normalize_text((base_renders.get(key) or {}).get("text") or "")
        cur_text = V.normalize_text((cur_renders.get(key) or {}).get("text") or "")
        if base_text != cur_text:
            drift2.append((key, _first_text_divergence(base_text, cur_text)))
    if mock_changed:
        _warn(checks, "ab.mock_render_end_to_end",
              "mock_bridge.py changed -> %d/%d live mock renders differ"
              % (len(drift2), len(mock_render_keys)), {"first": drift2[:2]})
    else:
        _check(checks, "ab.mock_render_end_to_end", not drift2,
               "%d/%d live mock renders differ: %s"
               % (len(drift2), len(mock_render_keys), drift2[:2])
               if drift2 else
               "all %d live mock renders identical" % len(mock_render_keys))

    base_edge = base.get("edge") or {}
    cur_edge = cur.get("edge") or {}
    edge_diff = {}
    for name in sorted(set(base_edge) | set(cur_edge)):
        a = base_edge.get(name) or {}
        b = cur_edge.get(name) or {}
        if a.get("error") or b.get("error"):
            if a.get("error") != b.get("error"):
                edge_diff[name] = [a.get("error"), b.get("error")]
            continue
        if V.normalize_text(a.get("text") or "") != V.normalize_text(
                b.get("text") or ""):
            edge_diff[name] = _first_text_divergence(a.get("text") or "",
                                                     b.get("text") or "")
    malformed = {k: v for k, v in edge_diff.items() if k != "malformed_nodes"}
    if malformed:
        checks.append({"name": "ab.edge_render_parity", "status": "INFO",
                       "detail": "malformed node render differs "
                                 "(implementation-defined): %s" % malformed,
                       "evidence": None})
    else:
        _check(checks, "ab.edge_render_parity", not edge_diff,
               "%d edge renders differ: %s"
               % (len(edge_diff), dict(list(edge_diff.items())[:2]))
               if edge_diff else
               "all %d edge renders identical" % len(base_edge))

    for side, capture in (("base", base), ("cur", cur)):
        failed = [c for c in (capture.get("checks") or [])
                  if c.get("status") == "FAIL"]
        status = "FAIL" if failed else "PASS"
        if side == "base" and failed:
            status = "WARN"
        checks.append({
            "name": "ab.side_checks.%s" % side, "status": status,
            "detail": "%d checks, %d failed: %s"
                      % (len(capture.get("checks") or []), len(failed),
                         [c["name"] for c in failed[:8]]),
            "evidence": None})
    return checks


def compare_real(base: dict, cur: dict) -> list:
    checks: list = []
    base_real = base.get("real") or {}
    cur_real = cur.get("real") or {}
    for hwnd in sorted(set(base_real) | set(cur_real)):
        if hwnd == "closed":
            continue
        a = base_real.get(hwnd) or {}
        b = cur_real.get(hwnd) or {}
        for view in sorted(set(a.get("snaps") or {}) | set(b.get("snaps") or {})):
            sa = (a.get("snaps") or {}).get(view)
            sb = (b.get("snaps") or {}).get(view)
            if sa is None or sb is None:
                _check(checks, "ab.real_snapshot[%s|%s]" % (hwnd, view), False,
                       "missing on one side")
                continue
            diffs = V.compare_snapshots(
                sa, sb,
                base_ids=(a.get("idmaps") or {}).get(view),
                cur_ids=(b.get("idmaps") or {}).get(view))
            focus_only = _focus_only_diffs(diffs)
            _check(checks, "ab.real_snapshot_parity[%s|%s]" % (hwnd, view),
                   not diffs or focus_only,
                   ("identical except volatile focused flags (%d)"
                    % len(diffs)) if focus_only else
                   ("%d diffs: %r" % (len(diffs), diffs[:4]) if diffs else
                    "identical (ids normalized by RuntimeId)"),
                   {"nodes": len(sa.get("nodes") or []),
                    "focus_only": focus_only})
        sa = ((a.get("snaps") or {}).get("controls") or {})
        sb = ((b.get("snaps") or {}).get("controls") or {})
        if sa and sb:
            _check(checks, "ab.real_meta[%s]" % hwnd,
                   sa.get("title") == sb.get("title")
                   and sa.get("rect") == sb.get("rect"),
                   "title=%r/%r rect=%r/%r"
                   % (sa.get("title"), sb.get("title"),
                      sa.get("rect"), sb.get("rect")))
    base_shot = base.get("screenshot") or {}
    cur_shot = cur.get("screenshot") or {}
    if base_shot and cur_shot:
        for key in ("scale",):
            a = (base_shot.get("window") or {}).get(key)
            b = (cur_shot.get("window") or {}).get(key)
            if a and b:
                _check(checks, "ab.screenshot.%s" % key, abs(a - b) < 0.01,
                       "base=%r cur=%r" % (a, b))
        for shot_key in ("window", "region"):
            a = (base_shot.get(shot_key) or {})
            b = (cur_shot.get(shot_key) or {})
            if a and b:
                _check(checks, "ab.screenshot.%s.geometry" % shot_key,
                       (a.get("screen_w"), a.get("screen_h"), a.get("origin"))
                       == (b.get("screen_w"), b.get("screen_h"), b.get("origin")),
                       "base=%r cur=%r"
                       % ((a.get("screen_w"), a.get("screen_h"), a.get("origin")),
                          (b.get("screen_w"), b.get("screen_h"), b.get("origin"))))
    base_mem = (base.get("memory") or {}).get("final") or {}
    cur_mem = (cur.get("memory") or {}).get("final") or {}
    if base_mem and cur_mem:
        for key in ("rid2id", "id2el", "cache", "win_ctl", "win_info"):
            a = base_mem.get(key)
            b = cur_mem.get(key)
            if a is None or b is None:
                continue
            growth = b - a
            _check(checks, "ab.memory.%s" % key,
                   not (a > 0 and b > a * 2 + 50),
                   "base=%r cur=%r (delta %+d)" % (a, b, growth))
    for side, capture in (("base", base), ("cur", cur)):
        failed = [c for c in (capture.get("checks") or [])
                  if c.get("status") == "FAIL"]
        _check(checks, "real_side.%s" % side, not failed,
               "%d checks, %d failed: %s"
               % (len(capture.get("checks") or []), len(failed),
                  [c["name"] for c in failed[:8]]))
    return checks


def compare_protocol(base: dict, cur: dict) -> list:
    checks: list = []
    _check(checks, "ab.proto.tools_schema_identical",
           (base.get("tools") or []) == (cur.get("tools") or []),
           "tools/list schemas differ"
           if (base.get("tools") or []) != (cur.get("tools") or []) else
           "identical", {"base_tools": len(base.get("tools") or [])})
    codes_a = base.get("codes") or {}
    codes_b = cur.get("codes") or {}
    code_diffs = {key: [codes_a.get(key), codes_b.get(key)]
                  for key in set(codes_a) | set(codes_b)
                  if codes_a.get(key) != codes_b.get(key)}
    _check(checks, "ab.proto.error_codes", not code_diffs, repr(code_diffs)[:400])
    side_failures = {}
    for side, capture in (("base", base), ("cur", cur)):
        side_failures[side] = [
            check["name"] for check in capture.get("checks") or []
            if check.get("name", "").startswith("proto")
            and check.get("status") == "FAIL"]
    _check(checks, "ab.proto.behaviour_checks",
           not side_failures.get("cur"),
           "current side protocol failures: %r" % (side_failures.get("cur"),))
    _check(checks, "ab.proto.stdout_purity",
           not (base.get("stdout_non_json") or cur.get("stdout_non_json")),
           "base=%r cur=%r" % (base.get("stdout_non_json"),
                               cur.get("stdout_non_json")))
    texts_a = base.get("texts") or {}
    texts_b = cur.get("texts") or {}
    text_diffs = {}
    for key in sorted(set(texts_a) | set(texts_b)):
        # cross-process: the observed window's foreground state is volatile, so
        # the " [focus]" header token is not part of the output contract here
        a = V.normalize_text(texts_a.get(key) or "", strip_focus=True)
        b = V.normalize_text(texts_b.get(key) or "", strip_focus=True)
        if a != b:
            text_diffs[key] = _first_text_divergence(a, b)
    if text_diffs:
        _warn(checks, "ab.proto.text_parity",
              "%d/%d model-facing text blocks differ (expected only if compact "
              "tuning is approved)" % (len(text_diffs), len(texts_a)),
              {"first": dict(list(text_diffs.items())[:4])})
    else:
        _check(checks, "ab.proto.text_parity", True,
               "all %d text blocks identical" % len(texts_a))
    checks.append({
        "name": "ab.proto.cold_start_info", "status": "INFO",
        "detail": "spawn->initialize base=%sms cur=%sms; spawn->tools/list "
                  "base=%sms cur=%sms; first-tool base=%sms cur=%sms"
                  % ((base.get("timings") or {}).get("spawn_to_initialize_ms"),
                     (cur.get("timings") or {}).get("spawn_to_initialize_ms"),
                     (base.get("timings") or {}).get("spawn_to_tools_list_ms"),
                     (cur.get("timings") or {}).get("spawn_to_tools_list_ms"),
                     (base.get("timings") or {}).get("first_tool_call_ms"),
                     (cur.get("timings") or {}).get("first_tool_call_ms")),
        "evidence": {"base": base.get("timings"), "cur": cur.get("timings")}})
    return checks


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def summarize(checks: list) -> dict:
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0, "INFO": 0}
    for check in checks:
        key = check.get("status", "INFO")
        counts[key] = counts.get(key, 0) + 1
    return counts


def write_markdown(path: str, checks: list, meta: dict) -> None:
    counts = summarize(checks)
    lines = []
    lines.append("# super-cu optimization - independent verification")
    lines.append("")
    lines.append("Generated %s by bench/verify/verify.py (verifier-owned)."
                 % meta.get("when"))
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    if counts["FAIL"]:
        verdict = "FAIL"
    elif counts["PASS"]:
        verdict = "PASS" if counts["WARN"] == 0 else "PASS (with warnings)"
    else:
        verdict = "NO-GATE-RUN"
    lines.append("**%s** - %d PASS / %d FAIL / %d WARN / %d SKIP / %d INFO"
                 % (verdict, counts["PASS"], counts["FAIL"], counts["WARN"],
                    counts["SKIP"], counts["INFO"]))
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    for key, value in (meta.get("environment") or {}).items():
        lines.append("- **%s**: %s" % (key, value))
    lines.append("")
    lines.append("## Baseline vs current")
    lines.append("")
    for key, value in (meta.get("sides") or {}).items():
        lines.append("- **%s**: %s" % (key, value))
    lines.append("")
    failures = [c for c in checks if c.get("status") == "FAIL"]
    if failures:
        lines.append("## Failures (must be fixed or explicitly waived)")
        lines.append("")
        for check in failures:
            lines.append("### %s" % check["name"])
            lines.append("")
            lines.append("    " + str(check.get("detail") or "")[:2000])
            lines.append("")
            if check.get("evidence"):
                lines.append("    " + json.dumps(check["evidence"],
                                                  ensure_ascii=False)[:2500])
                lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| # | check | status | evidence / detail |")
    lines.append("|---|-------|--------|-------------------|")
    for index, check in enumerate(checks, 1):
        detail = str(check.get("detail") or "").replace("\n", " ")
        detail = detail.replace("|", "/")[:240]
        lines.append("| %d | %s | %s | %s |"
                     % (index, check["name"], check.get("status"), detail))
    lines.append("")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    base_src = os.path.abspath(args.orig_dir)
    cur_src = os.path.abspath(args.src_dir)
    fixtures = os.path.abspath(args.fixtures)
    tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
    checks: list = []
    when = time.strftime("%Y-%m-%dT%H:%M:%S")
    print("[verify] run %s  base=%s  cur=%s" % (tag, base_src, cur_src))
    print("[verify] cpu:", _cpu())

    base_fmt = run_capture("fmt", base_src,
                           os.path.join(OUT_DIR, "base_fmt_%s.json" % tag),
                           fixtures=fixtures, label="base")
    cur_fmt = run_capture("fmt", cur_src,
                          os.path.join(OUT_DIR, "cur_fmt_%s.json" % tag),
                          fixtures=fixtures, label="cur")
    for side, data in (("base", base_fmt), ("cur", cur_fmt)):
        if data.get("fatal"):
            _check(checks, "capture.fmt.%s" % side, False, data["fatal"])
    checks += compare_fmt(base_fmt, cur_fmt,
                          allow_format_drift=args.allow_format_drift)
    checks += _inventory_ab(base_fmt.get("api") or {}, cur_fmt.get("api") or {})

    proto_base = protocol_mock(os.path.join(base_src, "server.py"), "base")
    proto_cur = protocol_mock(os.path.join(cur_src, "server.py"), "cur")
    checks += compare_protocol(proto_base, proto_cur)
    for side, data in (("base", proto_base), ("cur", proto_cur)):
        failed = [c["name"] for c in data.get("checks") or []
                  if c.get("status") == "FAIL"]
        _check(checks, "protocol_mock.side.%s" % side, not failed,
               "%d passed, %d failed: %s"
               % (len(data.get("checks") or []) - len(failed), len(failed),
                  failed[:8]))

    real_report = None
    if args.real:
        hwnds = [x for x in (args.hwnds or "").split(",") if x.strip()]
        if not hwnds:
            print("[verify] --real needs --hwnds (read-only target windows)")
            _skip(checks, "capture.real", "--real without --hwnds")
        else:
            hwnd_csv = ",".join(hwnds)
            base_real = run_capture(
                "real", base_src,
                os.path.join(OUT_DIR, "base_real_%s.json" % tag),
                hwnds=hwnd_csv, iters=args.iters, label="base",
                views=args.views, stale=args.stale, dialog=args.dialog)
            cur_real = run_capture(
                "real", cur_src,
                os.path.join(OUT_DIR, "cur_real_%s.json" % tag),
                hwnds=hwnd_csv, iters=args.iters, label="cur",
                views=args.views, stale=args.stale, dialog=args.dialog)
            for side, data in (("base", base_real), ("cur", cur_real)):
                if data.get("fatal"):
                    _check(checks, "capture.real.%s" % side, False, data["fatal"])
            checks += compare_real(base_real, cur_real)
            proto_real_base = protocol_real(
                os.path.join(base_src, "server.py"),
                [int(x) for x in hwnds], "base",
                prefetch_wait_ms=args.prefetch_wait_ms)
            proto_real_cur = protocol_real(
                os.path.join(cur_src, "server.py"),
                [int(x) for x in hwnds], "cur",
                prefetch_wait_ms=args.prefetch_wait_ms)
            checks += compare_protocol(proto_real_base, proto_real_cur)
            for side, data in (("base", proto_real_base), ("cur", proto_real_cur)):
                failed = [c["name"] for c in data.get("checks") or []
                          if c.get("status") == "FAIL"]
                _check(checks, "protocol_real.side.%s" % side, not failed,
                       "%d checks, %d failed: %s"
                       % (len(data.get("checks") or []), len(failed),
                          failed[:8]))
            if args.no_cache_switch:
                cur_nocache = run_capture(
                    "real", cur_src,
                    os.path.join(OUT_DIR, "cur_real_nocache_%s.json" % tag),
                    hwnds=hwnd_csv, iters=0, label="cur-nocache",
                    extra_env={"SUPER_CU_NO_CACHE": "1"})
                diffs = {}
                for hwnd in hwnds:
                    a = ((cur_real.get("real") or {}).get(hwnd) or {}).get(
                        "snaps", {}).get("controls")
                    b = ((cur_nocache.get("real") or {}).get(hwnd) or {}).get(
                        "snaps", {}).get("controls")
                    if not a or not b:
                        continue
                    found = V.compare_snapshots(
                        a, b,
                        base_ids=((cur_real.get("real") or {}).get(hwnd) or {}
                                  ).get("idmaps", {}).get("controls"),
                        cur_ids=((cur_nocache.get("real") or {}).get(hwnd) or {}
                                 ).get("idmaps", {}).get("controls"),
                        ignore_fields=("focused",))
                    if found:
                        diffs[hwnd] = found[:4]
                bridge_source = ""
                try:
                    with open(os.path.join(cur_src, "bridge.py"), "r",
                              encoding="utf-8", errors="replace") as fh:
                        bridge_source = fh.read()
                except OSError:
                    pass
                if "SUPER_CU_NO_CACHE" in bridge_source:
                    _check(checks, "ab.fallback.env_kill_switch", not diffs,
                           "subprocess with SUPER_CU_NO_CACHE=1 differs: %r"
                           % (diffs,) if diffs else
                           "legacy path identical with kill switch on")
                else:
                    _skip(checks, "ab.fallback.env_kill_switch",
                          "SUPER_CU_NO_CACHE not implemented in bridge.py")
            real_report = {"base": base_real, "cur": cur_real}
    else:
        _skip(checks, "capture.real", "not requested (--real)")

    counts = summarize(checks)
    print("[verify] %d PASS / %d FAIL / %d WARN / %d SKIP / %d INFO"
          % (counts["PASS"], counts["FAIL"], counts["WARN"], counts["SKIP"],
             counts["INFO"]))
    for check in checks:
        if check["status"] == "FAIL":
            print("  FAIL %s :: %s" % (check["name"],
                                       str(check.get("detail"))[:300]))
    meta = {
        "when": when, "tag": tag,
        "environment": {
            "host": "Windows 11, screen 2560x1600 @150%, 24 logical CPUs",
            "python": sys.version.split()[0],
            "cpu_at_start": _cpu(),
            "fixtures": fixtures,
            "fixture_count": len([f for f in os.listdir(fixtures)
                                  if f.endswith(".json")])
                             if os.path.isdir(fixtures) else 0,
            "load_note": "machine shared by several agents; timings are "
                         "covariates, not gates",
        },
        "sides": {
            "baseline (pristine)": base_src,
            "current (modified)": cur_src,
            "baseline module hashes": (base_fmt.get("meta") or {}).get(
                "module_sha256"),
            "current module hashes": (cur_fmt.get("meta") or {}).get(
                "module_sha256"),
        },
        "counts": counts,
        "real": bool(args.real),
    }
    summary = {"meta": meta, "checks": checks,
               "protocol": {"mock": {"base": proto_base, "cur": proto_cur}},
               "fmt": {"base": base_fmt, "cur": cur_fmt}}
    if real_report:
        summary["real"] = real_report
    json_path = os.path.join(OUT_DIR, "verify_%s.json" % tag)
    md_path = os.path.join(OUT_DIR, "verify_%s.md" % tag)
    V.write_json(json_path, summary)
    write_markdown(md_path, checks, meta)
    print("[verify] wrote", json_path)
    print("[verify] wrote", md_path)
    return 0 if counts["FAIL"] == 0 else 1


def cmd_protocol(args) -> int:
    server = os.path.join(os.path.abspath(args.src_dir), "server.py")
    if args.real:
        hwnds = [int(x) for x in (args.hwnds or "").split(",") if x.strip()]
        data = protocol_real(server, hwnds, "cur")
    else:
        data = protocol_mock(server, "cur")
    counts = summarize(data["checks"])
    print("[protocol] %d PASS / %d FAIL / %d WARN / %d SKIP"
          % (counts["PASS"], counts["FAIL"], counts["WARN"], counts["SKIP"]))
    for check in data["checks"]:
        if check["status"] == "FAIL":
            print("  FAIL %s :: %s" % (check["name"],
                                       str(check.get("detail"))[:300]))
    if args.out:
        V.write_json(args.out, data)
        print("[protocol] wrote", args.out)
    return 0 if counts["FAIL"] == 0 else 1




# --------------------------------------------------------------------------- #
# ship-blocker COM/prefetch race + cold-start A/B
# --------------------------------------------------------------------------- #
def _python_startup_ms() -> float:
    started = time.perf_counter()
    try:
        subprocess.run([sys.executable, "-c", "pass"], capture_output=True,
                       timeout=60)
    except Exception:
        return -1.0
    return round((time.perf_counter() - started) * 1000.0, 1)


def _measure_server(server_path: str, hwnd: int, wait_ms: int,
                    env_extra=None) -> dict:
    """One cold-start sample: spawn -> initialize -> tools/list -> first cu_show."""
    py_ms = _python_startup_ms()
    started = time.perf_counter()
    session = Session(server_path, [], env_extra=env_extra)
    out: dict = {"python_baseline_ms": py_ms}
    try:
        session.request("initialize", {"protocolVersion": "2024-11-05"},
                        mid="init")
        out["initialize_ms"] = round(
            (time.perf_counter() - started) * 1000.0, 1)
        session.request("tools/list", mid="tools")
        out["tools_list_ms"] = round(
            (time.perf_counter() - started) * 1000.0, 1)
        if wait_ms:
            time.sleep(wait_ms / 1000.0)
        show_started = time.perf_counter()
        response = session.request("tools/call", {
            "name": "cu_show",
            "arguments": {"window": "#%d" % hwnd, "view": "controls"}},
            mid="show", timeout=180)
        text, is_error, types = _tool_text(response)
        out["first_show_ms"] = round(
            (time.perf_counter() - show_started) * 1000.0, 1)
        out["first_show_is_error"] = is_error
        # MCP: no isError key means success, so normalise instead of == False
        out["first_show_ok"] = (is_error is not True and types == ["text"]
                                and bool(text))
        out["first_show_text"] = text[:300]
        out["first_show_chars"] = len(text)
        out["first_show_types"] = types
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            teardown = session.shutdown_gracefully(timeout=8)
            out["exit_code"] = teardown.get("exit_code")
        except Exception:
            session.kill()
    return out


def _median(values: list, *, allow_negative: bool = False) -> float:
    values = [value for value in values if isinstance(value, (int, float))
              and value is not None and (allow_negative or value >= 0)]
    if not values:
        return 0.0 if allow_negative else -1.0
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return round(float(values[mid]), 1)
    return round((values[mid - 1] + values[mid]) / 2.0, 1)


def _p95(values: list) -> float:
    values = sorted(value for value in values
                    if isinstance(value, (int, float)) and value is not None)
    if not values:
        return -1.0
    index = min(len(values) - 1, int(round(0.95 * (len(values) - 1))))
    return round(float(values[index]), 1)


def cmd_com_race(args) -> int:
    """Prefetch-thread COM init race: first real cu_show must not fail."""
    server = os.path.join(os.path.abspath(args.src_dir), "server.py")
    hwnds = [int(x) for x in (args.hwnds or "").split(",") if x.strip()]
    if not hwnds:
        print("[com-race] need --hwnds <live window>")
        return 2
    waits = [int(x) for x in (args.waits or "0,150,400,800").split(",")
             if x.strip()]
    checks: list = []
    payload: dict = {"server": server, "hwnds": hwnds,
                     "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "runs": {}, "checks": checks}
    for wait in waits:
        result = _measure_server(server, hwnds[0], wait)
        payload["runs"]["prefetch_wait_%d" % wait] = result
        ok = result.get("first_show_ok")
        is_error = result.get("first_show_is_error")
        text = result.get("first_show_text") or ""
        com_hit = "CoInitialize" in text or "尚未调用" in text
        _check(checks, "com_race.prefetch_wait_%d" % wait,
               ok is True,
               "isError=%r ok=%r first_show=%r%s"
               % (is_error, ok, text[:200],
                  " [CoInitialize signature]" if com_hit else ""),
               {"first_show_ms": result.get("first_show_ms")})
        if com_hit:
            payload.setdefault("com_error_signature", []).append(wait)
    for wait in (600,):
        result = _measure_server(server, hwnds[0], wait,
                                 env_extra={"SUPER_CU_NO_PREFETCH": "1"})
        payload["runs"]["no_prefetch_wait_%d" % wait] = result
        _check(checks, "com_race.no_prefetch_wait_%d" % wait,
               result.get("first_show_ok") is True,
               "isError=%r ok=%r text=%r"
               % (result.get("first_show_is_error"),
                  result.get("first_show_ok"),
                  (result.get("first_show_text") or "")[:200]))
    counts = summarize(checks)
    payload["counts"] = counts
    print("[com-race] %d PASS / %d FAIL" % (counts["PASS"], counts["FAIL"]))
    for check in checks:
        print("  %-4s %s :: %s" % (check["status"], check["name"],
                                   str(check.get("detail"))[:240]))
    if args.out:
        V.write_json(args.out, payload)
        print("[com-race] wrote", args.out)
    return 0 if counts["FAIL"] == 0 else 1


def cmd_coldstart(args) -> int:
    """Paired interleaved cold-start A/B: pristine vs current."""
    os.makedirs(OUT_DIR, exist_ok=True)
    base_server = os.path.join(os.path.abspath(args.orig_dir), "server.py")
    cur_server = os.path.join(os.path.abspath(args.src_dir), "server.py")
    hwnds = [int(x) for x in (args.hwnds or "").split(",") if x.strip()]
    if not hwnds:
        print("[coldstart] need --hwnds <live window>")
        return 2
    n = max(1, int(args.n or 7))
    wait_ms = int(args.wait_ms or 700)
    modes = [m.strip() for m in (args.modes or "prefetch,noprefetch").split(",")
             if m.strip()]
    payload: dict = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "n": n, "wait_ms": wait_ms, "hwnd": hwnds[0],
                     "cpu": _cpu(), "modes": {},
                     "note": "interleaved paired samples; python_baseline_ms is "
                             "the interpreter-only spawn cost measured in the "
                             "same window"}
    checks: list = []
    for mode in modes:
        env_extra = ({"SUPER_CU_NO_PREFETCH": "1"}
                     if mode == "noprefetch" else None)
        samples: dict = {"base": [], "cur": []}
        for index in range(n):
            order = ["base", "cur"] if index % 2 == 0 else ["cur", "base"]
            for side in order:
                server = base_server if side == "base" else cur_server
                result = _measure_server(server, hwnds[0], wait_ms,
                                         env_extra=env_extra)
                result["sample"] = index
                result["side"] = side
                result["mode"] = mode
                samples[side].append(result)
                payload.setdefault("runs", []).append(result)
        summary: dict = {}
        for side in ("base", "cur"):
            rows = samples[side]
            summary[side] = {
                "initialize_ms": _median([r.get("initialize_ms") for r in rows]),
                "tools_list_ms": _median([r.get("tools_list_ms") for r in rows]),
                "first_show_ms": _median([r.get("first_show_ms") for r in rows]),
                "initialize_p95": _p95([r.get("initialize_ms") for r in rows]),
                "first_show_p95": _p95([r.get("first_show_ms") for r in rows]),
                "python_baseline_ms": _median(
                    [r.get("python_baseline_ms") for r in rows]),
                "errors": sum(1 for r in rows
                              if r.get("first_show_ok") is not True),
                "com_errors": sum(
                    1 for r in rows
                    if "CoInitialize" in (r.get("first_show_text") or "")
                    or "尚未调用" in (r.get("first_show_text") or "")),
                "samples": len(rows),
            }
        deltas = {}
        base_rows = samples["base"]
        cur_rows = samples["cur"]
        for metric in ("initialize_ms", "tools_list_ms", "first_show_ms"):
            pairs = []
            for base_row, cur_row in zip(base_rows, cur_rows):
                a = base_row.get(metric)
                b = cur_row.get(metric)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    pairs.append(b - a)
            deltas[metric] = _median(pairs, allow_negative=True)
        summary["paired_delta_cur_minus_base"] = deltas
        payload["modes"][mode] = summary
        errors = summary["cur"]["errors"]
        _check(checks, "coldstart.%s.current_first_show" % mode, errors == 0,
               "%d/%d current first cu_show failed (COM errors=%d)"
               % (errors, summary["cur"]["samples"],
                  summary["cur"]["com_errors"]))
        _check(checks, "coldstart.%s.baseline_first_show" % mode,
               summary["base"]["errors"] == 0,
               "%d/%d baseline first cu_show failed"
               % (summary["base"]["errors"], summary["base"]["samples"]))
        net_init = (summary["cur"]["initialize_ms"]
                    - summary["cur"]["python_baseline_ms"])
        base_net = (summary["base"]["initialize_ms"]
                    - summary["base"]["python_baseline_ms"])
        checks.append({
            "name": "coldstart.%s.info" % mode, "status": "INFO",
            "detail": "initialize p50 base=%sms cur=%sms (net of python "
                      "startup: base=%sms cur=%sms); tools/list p50 "
                      "base=%sms cur=%sms; first cu_show p50 base=%sms cur=%sms; "
                      "paired delta initialize=%sms"
                      % (summary["base"]["initialize_ms"],
                         summary["cur"]["initialize_ms"],
                         round(base_net, 1), round(net_init, 1),
                         summary["base"]["tools_list_ms"],
                         summary["cur"]["tools_list_ms"],
                         summary["base"]["first_show_ms"],
                         summary["cur"]["first_show_ms"],
                         deltas["initialize_ms"]),
            "evidence": summary})
    counts = summarize(checks)
    payload["checks"] = checks
    payload["counts"] = counts
    print("[coldstart] %d PASS / %d FAIL / %d INFO"
          % (counts["PASS"], counts["FAIL"], counts["INFO"]))
    for check in checks:
        print("  %-4s %s :: %s" % (check["status"], check["name"],
                                   str(check.get("detail"))[:400]))
    if args.out:
        V.write_json(args.out, payload)
        print("[coldstart] wrote", args.out)
    return 0 if counts["FAIL"] == 0 else 1


def cmd_compare(args) -> int:
    base = V.read_json(args.base)
    cur = V.read_json(args.cur)
    checks = compare_fmt(base, cur, allow_format_drift=args.allow_format_drift)
    counts = summarize(checks)
    print("[compare] %d PASS / %d FAIL / %d WARN"
          % (counts["PASS"], counts["FAIL"], counts["WARN"]))
    for check in checks:
        if check["status"] in ("FAIL", "WARN"):
            print("  %-4s %s :: %s"
                  % (check["status"], check["name"],
                     str(check.get("detail"))[:300]))
    if args.md:
        write_markdown(args.md, checks, {
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "environment": {}, "sides": {}})
    return 0 if counts["FAIL"] == 0 else 1


def main(argv) -> int:
    _utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="full A/B run (fmt + protocol [ + real])")
    run.add_argument("--orig-dir", default=DEFAULT_ORIG)
    run.add_argument("--src-dir", default=DEFAULT_SRC)
    run.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    run.add_argument("--tag", default="")
    run.add_argument("--real", action="store_true")
    run.add_argument("--hwnds", default="")
    run.add_argument("--iters", type=int, default=0)
    run.add_argument("--views", default="controls,all,text")
    run.add_argument("--stale", action="store_true",
                     help="GUI: hwnd-reuse stale-id scenario (Notepad only)")
    run.add_argument("--dialog", action="store_true",
                     help="GUI: modal-dialog snapshot scenario (Notepad only)")
    run.add_argument("--prefetch-wait-ms", type=int, default=700,
                     help="wait after tools/list before the first real cu_show "
                          "(COM/prefetch regression)")
    run.add_argument("--no-cache-switch", action="store_true")
    run.add_argument("--allow-format-drift", action="store_true")

    proto = sub.add_parser("protocol", help="protocol purity/error-code test")
    proto.add_argument("--src-dir", default=DEFAULT_SRC)
    proto.add_argument("--mock", action="store_true", default=True)
    proto.add_argument("--real", action="store_true")
    proto.add_argument("--hwnds", default="")
    proto.add_argument("--out", default="")

    race = sub.add_parser("com-race",
                          help="ship-blocker: prefetch COM-init race on the "
                               "first real cu_show")
    race.add_argument("--src-dir", default=DEFAULT_SRC)
    race.add_argument("--hwnds", required=True)
    race.add_argument("--waits", default="0,150,400,800")
    race.add_argument("--out", default="")

    cold = sub.add_parser("coldstart",
                          help="paired interleaved cold-start A/B")
    cold.add_argument("--orig-dir", default=DEFAULT_ORIG)
    cold.add_argument("--src-dir", default=DEFAULT_SRC)
    cold.add_argument("--hwnds", required=True)
    cold.add_argument("--n", type=int, default=7)
    cold.add_argument("--wait-ms", type=int, default=700)
    cold.add_argument("--modes", default="prefetch,noprefetch")
    cold.add_argument("--out", default="")

    cmp_parser = sub.add_parser("compare", help="compare two fmt captures")
    cmp_parser.add_argument("--base", required=True)
    cmp_parser.add_argument("--cur", required=True)
    cmp_parser.add_argument("--md", default="")
    cmp_parser.add_argument("--allow-format-drift", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "protocol":
        return cmd_protocol(args)
    if args.cmd == "com-race":
        return cmd_com_race(args)
    if args.cmd == "coldstart":
        return cmd_coldstart(args)
    if args.cmd == "compare":
        return cmd_compare(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

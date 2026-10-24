#!/usr/bin/env python3
"""super-cu T2 self-test: MCP protocol + compact formatting + boundaries.

    python tests/test_server.py            # mock protocol tests + real if bridge.py exists
    python tests/test_server.py --mock-only
    python tests/test_server.py --real     # force the real-bridge regression

The protocol half speaks to ``src/server.py`` exactly like DSH does: newline
delimited JSON-RPC over a pipe.  Every byte the server writes to stdout is
parsed with ``json.loads`` and a single failure is reported.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
SERVER = os.path.join(SRC, "server.py")
PY = sys.executable
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import compact  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    if ok:
        PASSED.append(name)
        print(f"PASS {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
# interactive server session (like a DSH MCP client)
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, args=("--mock",)):
        self.errfile = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [PY, "-u", SERVER, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errfile,
            bufsize=0,
        )
        self.next_id = 0
        self.raw_stdout = []
        self.bad_lines = []
        self.response_count = 0

    # -- plumbing ---------------------------------------------------------- #
    def _write(self, text):
        self.proc.stdin.write((text + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read_line(self, timeout=90):
        box = {}

        def worker():
            try:
                box["line"] = self.proc.stdout.readline()
            except Exception as exc:  # pragma: no cover
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError(f"server did not answer in {timeout}s "
                               f"(stderr: {self.stderr_text()[-800:]})")
        if "error" in box:
            raise box["error"]
        line = box["line"]
        if not line:
            raise RuntimeError("server closed stdout "
                               f"(stderr: {self.stderr_text()[-800:]})")
        text = line.decode("utf-8", "replace").rstrip("\n")
        if text.strip():
            self.raw_stdout.append(text)
        return text

    def _parse(self, text):
        try:
            return json.loads(text)
        except Exception as exc:
            self.bad_lines.append((text, str(exc)))
            return None

    # -- protocol ---------------------------------------------------------- #
    def send(self, method, params=None, timeout=90):
        self.next_id += 1
        mid = self.next_id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(json.dumps(msg, ensure_ascii=False))
        text = self._read_line(timeout)
        response = self._parse(text)
        if response is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": "unparsable", "message": text}}
        self.response_count += 1
        return response

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(json.dumps(msg, ensure_ascii=False))

    def send_raw(self, raw, timeout=90):
        self._write(raw)
        text = self._read_line(timeout)
        response = self._parse(text)
        if response is not None:
            self.response_count += 1
        return response

    def call(self, name, arguments, timeout=120):
        return self.send("tools/call", {"name": name, "arguments": arguments},
                         timeout=timeout)

    # -- teardown ---------------------------------------------------------- #
    def stderr_text(self):
        try:
            self.errfile.flush()
            self.errfile.seek(0)
            return self.errfile.read().decode("utf-8", "replace")
        except Exception:
            return ""

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        # drain anything the server wrote after the last response
        try:
            rest = self.proc.stdout.read().decode("utf-8", "replace")
        except Exception:
            rest = ""
        for line in rest.splitlines():
            if line.strip():
                self.raw_stdout.append(line)
                if self._parse(line) is None:
                    pass
        err = self.stderr_text()
        self.errfile.close()
        return err


def text_of(response):
    result = (response or {}).get("result") or {}
    blocks = result.get("content") or []
    for block in blocks:
        if block.get("type") == "text":
            return block.get("text") or ""
    return ""


def is_error(response):
    return bool(((response or {}).get("result") or {}).get("isError"))


def find_id(text, ct, name):
    pattern = r"\[(\d+)\] " + re.escape(ct) + r' "' + re.escape(name)
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def obs_ms(text):
    match = re.search(r"(\d+)ms", text)
    return int(match.group(1)) if match else -1


MUCH = 10830
CONTROL_LIMIT = 3500


# --------------------------------------------------------------------------- #
# 1. protocol layer
# --------------------------------------------------------------------------- #
def test_protocol():
    section("protocol / stdout purity (mock)")
    session = Session()
    try:
        # -- initialize ---------------------------------------------------- #
        init = session.send("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "dsh", "version": "test"},
        })
        result = init.get("result") or {}
        check("initialize echoes protocolVersion",
              result.get("protocolVersion") == "2025-06-18",
              str(result.get("protocolVersion")))
        check("initialize capabilities.tools == {}",
              result.get("capabilities") == {"tools": {}})
        check("initialize serverInfo",
              result.get("serverInfo") == {"name": "supercu", "version": "1.0.0"},
              str(result.get("serverInfo")))

        second = session.send("initialize", {})
        check("initialize defaults protocolVersion to 2024-11-05",
              (second.get("result") or {}).get("protocolVersion") == "2024-11-05",
              str((second.get("result") or {}).get("protocolVersion")))

        # -- notification is silent ---------------------------------------- #
        session.notify("notifications/initialized", {})
        pong = session.send("ping")
        check("notifications/initialized produces no response",
              pong.get("id") == session.next_id and pong.get("result") == {},
              f'id={pong.get("id")} expected={session.next_id}')

        # -- tools/list ---------------------------------------------------- #
        listing = session.send("tools/list")
        tools = (listing.get("result") or {}).get("tools") or []
        names = [tool.get("name") for tool in tools]
        check("tools/list exposes exactly cu_show + cu_do",
              names == ["cu_show", "cu_do"], str(names))
        by_name = {tool.get("name"): tool for tool in tools}
        show_schema = (by_name.get("cu_show") or {}).get("inputSchema") or {}
        props = show_schema.get("properties") or {}
        check("cu_show schema has all contract properties",
              {"window", "scope", "view", "filter", "max_nodes", "screenshot",
               "region", "max_image_width"} <= set(props),
              str(sorted(props)))
        check("cu_show screenshot defaults to false",
              props.get("screenshot", {}).get("default") is False)
        check("cu_show view enum", props.get("view", {}).get("enum")
              == ["controls", "text", "all"])
        do_schema = (by_name.get("cu_do") or {}).get("inputSchema") or {}
        do_props = do_schema.get("properties") or {}
        check("cu_do requires steps", do_schema.get("required") == ["steps"])
        step_props = (do_props.get("steps") or {}).get("items") or {}
        check("cu_do step requires op",
              (step_props.get("required") or []) == ["op"])
        op_enum = (step_props.get("properties") or {}).get("op", {}).get("enum") or []
        check("cu_do op enum covers contract ops",
              {"click", "set", "type", "key", "launch", "close", "wait",
               "assert", "drag", "paste"} <= set(op_enum), str(len(op_enum)))
        check("cu_do observe defaults to auto",
              do_props.get("observe", {}).get("default") == "auto")

        # -- unknown method / malformed JSON ------------------------------- #
        unknown = session.send("totally/unknown")
        check("unknown method -> -32601",
              (unknown.get("error") or {}).get("code") == -32601,
              str(unknown.get("error")))
        broken = session.send_raw("{this is not json")
        check("malformed JSON -> -32700",
              (broken.get("error") or {}).get("code") == -32700,
              str((broken or {}).get("error")))
        bad_tool = session.call("cu_nope", {})
        check("unknown tool -> -32602",
              (bad_tool.get("error") or {}).get("code") == -32602,
              str((bad_tool or {}).get("error")))

        # -- cu_show: foreground Edge, 15 tabs ----------------------------- #
        started = time.perf_counter()
        show = session.call("cu_show", {})
        wall = int((time.perf_counter() - started) * 1000)
        text = text_of(show)
        check("cu_show returns text content block",
              bool(text) and (show.get("result") or {}).get("content"),
              f"{len(text)} chars")
        check("cu_show header names the foreground window",
              text.startswith("win #1002"), text.splitlines()[0][:80])
        check("cu_show header has obs/ms/view line",
              bool(re.search(r"^obs \d+n/\d+ shown \d+ms controls$",
                             text, re.M)), text.splitlines()[1][:80] if "\n" in text else "")
        check("cu_show keeps all 15 tabitems (recall)",
              text.count("tabitem") == 15,
              f"{text.count('tabitem')} tabitems")
        check("Edge 15-tab text under controls target",
              len(text) <= CONTROL_LIMIT, f"{len(text)} chars, {wall}ms wall")
        check("cu_show marks the focused window", "[focus]" in text)
        check("cu_show uses one-letter capability codes",
              bool(re.search(r"\[[etsvic]\]", text)))
        check("cu_show uses single-char state markers",
              bool(re.search(r"\[[etsvic]\] [+*-]", text)))

        # -- desktop scope ------------------------------------------------- #
        desktop = text_of(session.call("cu_show", {"scope": "desktop"}))
        check("scope=desktop lists windows", desktop.startswith("desktop "),
              desktop.splitlines()[0])
        check("scope=desktop marks focus", "[focus]" in desktop)
        check("scope=desktop exposes #hwnd",
              desktop.count("#10") >= 5, str(desktop.count("#10")))

        # -- filter -------------------------------------------------------- #
        matched = text_of(session.call("cu_show", {"filter": "刷新"}))
        check("filter keeps matching element", 'button "刷新"' in matched)
        empty = text_of(session.call("cu_show", {"filter": "zzz-no-such"}))
        check("filter with no match is human readable",
              "0 matched" in empty and "没有匹配" in empty, empty.splitlines()[-1])
        check("filter with no match does not set isError",
              not is_error(session.call("cu_show", {"filter": "zzz-no-such"})))

        # -- max_nodes out of range --------------------------------------- #
        huge = session.call("cu_show", {"window": "#1007", "max_nodes": 10 ** 9})
        huge_text = text_of(huge)
        check("huge max_nodes clamped, no crash",
              bool(huge_text) and huge_text.startswith("win #1007"),
              f"{len(huge_text)} chars")
        check("controls target caps text", len(huge_text) <= CONTROL_LIMIT,
              str(len(huge_text)))
        check("hard cap is 10830 chars", len(huge_text) <= MUCH,
              str(len(huge_text)))
        check("cropping reports hidden count", "hidden" in huge_text)

        # -- screenshot ---------------------------------------------------- #
        shot = session.call("cu_show", {"window": "#1001", "screenshot": True})
        blocks = (shot.get("result") or {}).get("content") or []
        images = [block for block in blocks if block.get("type") == "image"]
        shot_text = text_of(shot)
        check("screenshot adds an image block", len(images) == 1, str(len(images)))
        if images:
            image = images[0]
            data = base64.b64decode(image.get("data") or "")
            check("image mimeType is jpeg", image.get("mimeType") == "image/jpeg",
                  str(image.get("mimeType")))
            check("image decodes to JPEG magic",
                  data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9",
                  f"{len(data)} bytes")
            check("JPEG long side <= 1728",
                  shot_text.split("(")[0].find("img ") >= 0
                  and _img_long_side(shot_text) <= 1728,
                  str(_img_long_side(shot_text)))
        check("screenshot text tells model to use @n", "@n:" in shot_text)
        check("screenshot text has no scale arithmetic hint",
              "scale" not in shot_text.lower(), shot_text.splitlines()[-1][:80])

        # -- cu_do using #id discovered in this same process --------------- #
        notepad = text_of(session.call("cu_show", {"window": "#1001"}))
        edit_id = find_id(notepad, "edit", "文本编辑器")
        check("cu_show #1001 exposes the edit element", edit_id is not None,
              str(edit_id))
        if edit_id:
            set_result = session.call("cu_do", {"steps": [
                {"op": "set", "target": f"#{edit_id}", "text": "hello super-cu"}]})
            set_text = text_of(set_result)
            check("cu_do set by #id ok", not is_error(set_result)
                  and set_text.startswith("1 ok set"), set_text.splitlines()[0][:90])
            check("cu_do default observe=auto returns new state",
                  'val="hello super-cu"' in set_text)

            batch = session.call("cu_do", {"steps": [
                {"op": "set", "target": f"#{edit_id}", "text": "batch-1"},
                {"op": "type", "target": f"#{edit_id}", "text": "+2",
                 "expect": "batch-1+2"},
                {"op": "assert", "expect": "batch-1+2"},
            ]})
            batch_text = text_of(batch)
            check("cu_do 3-step batch all ok",
                  not is_error(batch) and batch_text.count(" ok ") == 3,
                  batch_text.splitlines()[0][:90] if batch_text else "")
            check("expect adds exp✓", "exp✓" in batch_text)

        # -- @n / @img mapping (last shot was #1001) ----------------------- #
        point_ok = session.call("cu_do", {"steps": [
            {"op": "click", "target": "@n:0.5,0.5"}]})
        point_text = text_of(point_ok)
        check("@n normalized coordinate maps to a real element",
              not is_error(point_ok) and "1 ok click" in point_text,
              point_text.splitlines()[0][:90])
        img_point = session.call("cu_do", {"steps": [
            {"op": "click", "target": "@img:600,300"}]})
        check("@img pixel coordinate maps too", not is_error(img_point),
              text_of(img_point).splitlines()[0][:90])

        # -- wait ---------------------------------------------------------- #
        waited = session.call("cu_do", {"steps": [
            {"op": "wait", "expect": "未命名", "timeout_ms": 500}]})
        check("op=wait with expect ok", not is_error(waited),
              text_of(waited).splitlines()[0][:90])

        # -- wait_kind (P1-3): change / gone / window are expressible -------- #
        changed_wait = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "change", "timeout_ms": 500}]})
        check("wait_kind=change accepted", not is_error(changed_wait),
              text_of(changed_wait).splitlines()[0][:90])
        gone_wait = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "gone", "target": "#999999",
             "timeout_ms": 500}]})
        check("wait_kind=gone accepted", not is_error(gone_wait),
              text_of(gone_wait).splitlines()[0][:90])
        window_wait = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "window", "text": "未命名",
             "timeout_ms": 500}]})
        check("wait_kind=window accepted", not is_error(window_wait),
              text_of(window_wait).splitlines()[0][:90])
        bad_wait = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "nonsense"}]})
        check("invalid wait_kind -> -32602",
              (bad_wait.get("error") or {}).get("code") == -32602,
              str(bad_wait.get("error"))[:90])

        # -- task-5: wait_kind=window accepts window= and carries the hit ---- #
        window_only = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "window", "window": "题目收集平台",
             "timeout_ms": 500}]})
        window_only_text = text_of(window_only)
        check("wait_kind=window accepts window= (no text needed)",
              not is_error(window_only)
              and "窗口已出现" in window_only_text,
              window_only_text.splitlines()[0][:100])
        check("wait_kind=window detail carries title + hwnd",
              "#1002" in window_only_text,
              window_only_text.splitlines()[0][:100])
        kind_alias = session.call("cu_do", {"steps": [
            {"op": "wait", "kind": "window", "text": "题目收集平台",
             "timeout_ms": 500}]})
        check("`kind` works as wait_kind alias",
              not is_error(kind_alias)
              and "窗口已出现" in text_of(kind_alias),
              text_of(kind_alias).splitlines()[0][:100])

        # -- task-6: wait must actually wait, not pre-resolve and fail fast ---- #
        # (mock caps its simulated wait at 250ms; real bridge honours timeout_ms)
        t0 = time.perf_counter()
        missing_window = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "window",
             "window": "NO_SUCH_WINDOW_XYZ_2026", "timeout_ms": 600}]})
        missing_window_ms = int((time.perf_counter() - t0) * 1000)
        missing_text = text_of(missing_window)
        check("wait_kind=window for a missing window waits the timeout",
              is_error(missing_window) and missing_window_ms >= 150,
              f"{missing_window_ms}ms; {missing_text.splitlines()[0][:90]}")
        check("missing-window detail reports the timeout, not '没有找到窗口'",
              "未等到窗口" in missing_text and "没有找到窗口" not in missing_text,
              missing_text.splitlines()[0][:90])

        t0 = time.perf_counter()
        existing_window = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "window", "window": "题目收集平台",
             "timeout_ms": 500}]})
        existing_ms = int((time.perf_counter() - t0) * 1000)
        existing_text = text_of(existing_window)
        check("wait_kind=window for an existing window returns immediately",
              not is_error(existing_window) and existing_ms < 400,
              f"{existing_ms}ms")
        check("existing-window detail has #hwnd and title",
              "#1002" in existing_text and "题目收集平台" in existing_text,
              existing_text.splitlines()[0][:100])

        t0 = time.perf_counter()
        missing_element = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "element", "target": "#999999",
             "timeout_ms": 600}]})
        missing_element_ms = int((time.perf_counter() - t0) * 1000)
        check("wait_kind=element for a missing element waits the timeout",
              is_error(missing_element) and missing_element_ms >= 150,
              f"{missing_element_ms}ms; "
              f"{text_of(missing_element).splitlines()[0][:90]}")

        # gone/change share the same pre-resolve bug path; a missing window
        # spec must not abort them before the wait loop runs
        gone_wait2 = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "gone", "window": "NO_SUCH_WINDOW_XYZ",
             "timeout_ms": 600}]})
        check("wait_kind=gone no longer pre-resolves a missing window",
              not is_error(gone_wait2),
              text_of(gone_wait2).splitlines()[0][:90])
        change_wait2 = session.call("cu_do", {"steps": [
            {"op": "wait", "wait_kind": "change", "window": "NO_SUCH_WINDOW_XYZ",
             "timeout_ms": 600}]})
        check("wait_kind=change no longer pre-resolves a missing window",
              not is_error(change_wait2),
              text_of(change_wait2).splitlines()[0][:90])

        # -- task-5: paste forwards text/value into the clipboard path ------- #
        paste_missing = session.call("cu_do", {"steps": [
            {"op": "paste", "window": "#1001"}]})
        check("paste without text/value -> -32602, no blind Ctrl+V",
              (paste_missing.get("error") or {}).get("code") == -32602
              and "text" in ((paste_missing.get("error") or {}).get("message") or ""),
              str(paste_missing.get("error"))[:100])
        paste_ok = session.call("cu_do", {"steps": [
            {"op": "paste", "text": "剪贴板测试", "window": "#1001"}]})
        check("paste with text succeeds", not is_error(paste_ok),
              text_of(paste_ok).splitlines()[0][:90])
        pasted = text_of(session.call("cu_show", {"window": "#1001"}))
        check("paste wrote the given text (not stale clipboard)",
              'val="剪贴板测试"' in pasted,
              [l for l in pasted.splitlines() if "val=" in l][:1])
        paste_alias = session.call("cu_do", {"steps": [
            {"op": "paste", "value": "value别名", "window": "#1001"}]})
        check("paste accepts value= alias", not is_error(paste_alias),
              text_of(paste_alias).splitlines()[0][:90])

        # -- P1-1: inline observe keeps the cu_show budget ------------------ #
        probe = session.call("cu_show", {"window": "#1007", "max_nodes": 20})
        probe_text = text_of(probe)
        big_button = find_id(probe_text, "button", "元素 0 的标题")
        if big_button:
            state_batch = session.call("cu_do", {"steps": [
                {"op": "click", "target": f"#{big_button}",
                 "window": "#1007"}]})
            state_text = text_of(state_batch)
            check("P1-1 inline observe bounded (<=3600 chars)",
                  len(state_text) <= 3600,
                  f"{len(state_text)} chars / {len(state_text.splitlines())} lines")
            check("P1-1 inline observe bounded (<=115 lines)",
                  len(state_text.splitlines()) <= 115,
                  str(len(state_text.splitlines())))
            check("P1-1 inline observe present after a change",
                  "obs " in state_text and "win #1007" in state_text)
            no_change = session.call("cu_do", {"steps": [
                {"op": "focus", "target": f"#{big_button}",
                 "window": "#1007"}]})
            no_change_text = text_of(no_change)
            check("P1-1 no state when nothing changed",
                  "win #" not in no_change_text,
                  no_change_text.splitlines()[0][:80] if no_change_text else "")

        # -- boundaries: empty steps / invalid op / stale id -------------- #
        empty_steps = session.call("cu_do", {"steps": []})
        check("empty steps -> -32602 with advice",
              (empty_steps.get("error") or {}).get("code") == -32602
              and "不能为空" in ((empty_steps.get("error") or {}).get("message") or ""),
              str(empty_steps.get("error")))
        invalid_op = session.call("cu_do", {"steps": [{"op": "frobnicate"}]})
        check("invalid op -> -32602 listing valid ops",
              (invalid_op.get("error") or {}).get("code") == -32602
              and "click" in ((invalid_op.get("error") or {}).get("message") or ""),
              str(invalid_op.get("error"))[:90])
        stale = session.call("cu_do", {"steps": [
            {"op": "click", "target": "#999999"}]})
        stale_text = text_of(stale)
        check("nonexistent #id -> isError + advice, no crash",
              is_error(stale) and "FAIL" in stale_text and "→" in stale_text,
              stale_text.splitlines()[0][:90])
        bad_region = session.call("cu_show", {"region": [1, 2, 3]})
        check("bad region -> -32602",
              (bad_region.get("error") or {}).get("code") == -32602,
              str(bad_region.get("error"))[:80])
        bad_view = session.call("cu_show", {"view": "everything"})
        check("bad view enum -> -32602",
              (bad_view.get("error") or {}).get("code") == -32602,
              str(bad_view.get("error"))[:80])

        # -- launch / close ------------------------------------------------ #
        launched = session.call("cu_do", {"steps": [
            {"op": "launch", "app": "notepad"},
            {"op": "close", "window": "#1001"}]})
        check("cu_do launch+close batch ok",
              not is_error(launched)
              and text_of(launched).count(" ok ") == 2,
              text_of(launched).splitlines()[0][:90] if text_of(launched) else "")
        gone = session.call("cu_show", {"window": "#1001"})
        check("closed window gives actionable error",
              is_error(gone) and "没有找到窗口" in text_of(gone),
              text_of(gone).splitlines()[0][:90])

    finally:
        stderr = session.close()

    check("stdout contained zero non-JSON lines", session.bad_lines == [],
          str(session.bad_lines[:2]))
    check("every stdout line was consumed as a response",
          len(session.raw_stdout) == session.response_count,
          f"lines={len(session.raw_stdout)} responses={session.response_count}")
    check("server logged to stderr with [supercu] prefix",
          "[supercu]" in stderr, stderr.splitlines()[0][:80] if stderr else "")
    check("stderr does not leak into stdout",
          not any("[supercu]" in line for line in session.raw_stdout))


def _img_long_side(text):
    match = re.search(r"img (\d+)x(\d+)", text)
    if not match:
        return 10 ** 9
    return max(int(match.group(1)), int(match.group(2)))


# --------------------------------------------------------------------------- #
# 2. compact formatting unit tests (in-process, no desktop)
# --------------------------------------------------------------------------- #
def _node(nid, ct="pane", name="", rect=(0, 0, 10, 10), depth=0, **kwargs):
    node = {"id": nid, "ct": ct, "name": name, "rect": list(rect),
            "depth": depth, "enabled": True, "offscreen": False,
            "focused": False, "patterns": [], "kids": 0, "val": None}
    node.update(kwargs)
    return node


def test_compact():
    section("compact formatting (compaction v2)")
    snap = {
        "hwnd": 42, "title": "题目收集平台 - Microsoft Edge",
        "rect": [0, 0, 2560, 1600], "focused": True, "elapsed_ms": 132,
        "total": 3, "shown": 3, "truncated": False,
        "nodes": [
            _node(1, "tabitem", "对话", (10, 10, 100, 40), 0,
                  patterns=["selection", "invoke"], selected=True),
            _node(2, "edit", "搜索\n框\n", (10, 50, 300, 80), 1, val="dsh"),
            _node(3, "button", "保存", (10, 90, 100, 120), 1,
                  patterns=["invoke"], enabled=False),
        ],
    }
    text = compact.format_snapshot(snap)
    check("header line matches contract", text.splitlines()[0].startswith(
        'win #42 "题目收集平台 - Microsoft Edge" 2560x1600 @0,0 [focus]'),
        text.splitlines()[0])
    check("obs line matches contract",
          text.splitlines()[1] == "obs 3n/3 shown 132ms controls",
          text.splitlines()[1])
    check("selection uses one-letter code + '+' state",
          '[s] +' in text, [l for l in text.splitlines() if "[s]" in l][:1])
    check("disabled uses '-' state", '[i] -' in text)
    check("val marker", 'val="dsh"' in text)
    check("newlines squeezed to ⏎", 'edit "搜索⏎框' in text)
    check("indent is one space per level",
          any(line.startswith(" ") and not line.startswith("  ")
              for line in text.splitlines()[2:]))

    long_name = "超长名字" * 40
    long_snap = {"hwnd": 7, "title": "t", "rect": [0, 0, 100, 100],
                 "nodes": [_node(9, "button", long_name)]}
    long_text = compact.format_snapshot(long_snap)
    rendered = re.search(r'\[\d+\] button "([^"]*)"', long_text).group(1)
    check("names truncate at 48 chars with …",
          len(rendered) <= 48 and rendered.endswith("…"), f"{len(rendered)} chars")

    deep_nodes = []
    for i in range(0, 8):
        deep_nodes.append(_node(i, "pane", f"层{i}", (0, i, 10, i + 1), i,
                                path=f"父层{max(0, i - 1)}"))
    deep = {"hwnd": 8, "title": "t", "rect": [0, 0, 100, 100],
            "nodes": deep_nodes}
    deep_text = compact.format_snapshot(deep)
    deep_body = [line for line in deep_text.splitlines()[2:] if "[pane" not in line]
    max_indent = max((len(line) - len(line.lstrip(" ")) for line in deep_body),
                     default=0)
    check("indent caps at 3 levels (3 spaces)", max_indent == 3, str(max_indent))
    check("deep levels flatten behind › breadcrumb", "›" in deep_text)

    # only repetitive content lists fold; navigation targets never do
    tab_nodes = [_node(100 + i, "tabitem", f"标签{i}", (i * 10, 0, i * 10 + 5, 5),
                       1, patterns=["selection"], selected=(i == 11))
                 for i in range(15)]
    tab_text = compact.format_snapshot({"hwnd": 3, "title": "t",
                                        "rect": [0, 0, 100, 100],
                                        "nodes": tab_nodes})
    check("15 tabitems stay unfolded for interactive recall",
          tab_text.count("tabitem") == 15 and "more" not in tab_text,
          f"{tab_text.count('tabitem')} lines")

    button_nodes = [_node(200 + i, "button", f"操作{i}", (i * 10, 0, i * 10 + 5, 5),
                          1, patterns=["invoke"])
                    for i in range(15)]
    button_text = compact.format_snapshot({"hwnd": 3, "title": "t",
                                           "rect": [0, 0, 100, 100],
                                           "nodes": button_nodes})
    check("15 buttons stay unfolded too",
          button_text.count("button") == 15, f"{button_text.count('button')} lines")

    list_nodes = [_node(300 + i, "listitem", f"条目{i}", (i * 10, 0, i * 10 + 5, 5),
                        1, patterns=["selection"]) for i in range(15)]
    fold_text = compact.format_snapshot({"hwnd": 3, "title": "t",
                                         "rect": [0, 0, 100, 100],
                                         "nodes": list_nodes})
    check("15 listitems fold to 8 + marker",
          fold_text.count("listitem") == 8 and "…+7 more" in fold_text,
          f"{fold_text.count('listitem')} lines")
    check("fold marker suggests a filter keyword",
          "…+7 more (filter:" in fold_text)

    # shadow text: text duplicating an interactive ancestor is removed
    shadow = {"hwnd": 4, "title": "t", "rect": [0, 0, 100, 100], "nodes": [
        _node(1, "treeitem", "opencode", (0, 0, 100, 20), 0,
              patterns=["selection"]),
        _node(2, "text", "opencode", (0, 0, 100, 20), 1),
        _node(3, "button", "确定", (0, 30, 100, 50), 1, patterns=["invoke"]),
    ]}
    shadow_text = compact.format_snapshot(shadow)
    check("shadow text duplicating an ancestor is dropped",
          "[2] text" not in shadow_text and "[3] button" in shadow_text)

    # offscreen pure text is dropped, offscreen interactive is kept
    offscreen = {"hwnd": 5, "title": "t", "rect": [0, 0, 100, 100], "nodes": [
        _node(1, "text", "滚出视口的一大段内容", (0, 0, 100, 20), 0,
              offscreen=True),
        _node(2, "button", "重试", (0, 30, 100, 50), 1, patterns=["invoke"],
              offscreen=True),
    ]}
    offscreen_text = compact.format_snapshot(offscreen)
    check("offscreen text dropped", "滚出视口" not in offscreen_text)
    check("offscreen interactive kept", "[2] button" in offscreen_text
          and "~" in offscreen_text)

    # name-less container is hoisted away
    container = {"hwnd": 6, "title": "t", "rect": [0, 0, 100, 100], "nodes": [
        _node(1, "pane", "", (0, 0, 100, 100), 0, kids=1),
        _node(2, "button", "提交", (0, 30, 100, 50), 1, patterns=["invoke"]),
    ]}
    container_text = compact.format_snapshot(container)
    check("name-less container dropped, child hoisted",
          "[1] pane" not in container_text
          and any(line.startswith("[2] button") for line in container_text.splitlines()))

    # window chrome dropped, page-level 关闭 button kept
    chrome = {"hwnd": 7, "title": "t", "rect": [0, 0, 100, 100], "nodes": [
        _node(1, "button", "最小化", (0, 0, 20, 20), 1, patterns=["invoke"],
              cls="WinCaptionButton"),
        _node(2, "button", "关闭", (80, 0, 100, 20), 4, patterns=["invoke"],
              cls="Chrome_Button"),
        _node(3, "button", "确定", (0, 30, 100, 50), 1, patterns=["invoke"]),
    ]}
    chrome_text = compact.format_snapshot(chrome)
    check("caption button dropped as chrome", "[1] button" not in chrome_text)
    check("page-level 关闭 button kept", "[2] button" in chrome_text)

    big_nodes = [_node(1000 + i, "button" if i % 2 else "text",
                       f"元素{i}", (i % 40, i // 40, i % 40 + 5, i // 40 + 5), 1,
                       patterns=["invoke"] if i % 2 else [])
                 for i in range(5000)]
    big = {"hwnd": 9, "title": "big", "rect": [0, 0, 2560, 1600],
           "total": 5000, "shown": 5000, "nodes": big_nodes}
    big_text = compact.format_snapshot(big)
    check("5000 nodes cropped to controls target", len(big_text) <= CONTROL_LIMIT,
          str(len(big_text)))
    check("overflow reports …(+N hidden)", "hidden" in big_text)
    check("interactive codes survive cropping", "[i]" in big_text)

    many = [_node(5000 + i, "button", f"按钮{i}", (i % 30, i // 30, i % 30 + 3,
                                                   i // 30 + 3), 1,
                  patterns=["invoke"]) for i in range(200)]
    capped = compact.format_snapshot({"hwnd": 12, "title": "t",
                                      "rect": [0, 0, 100, 100],
                                      "nodes": many}, max_lines=40)
    element_lines = [line for line in capped.splitlines()[2:]
                     if line.startswith("[") or line.startswith(" ")]
    check("max_lines caps rendered element lines", len(element_lines) <= 41,
          str(len(element_lines)))
    check("line-capped output still reports hidden", "hidden" in capped)

    # -- cut markers (B3 audit fix): tree / budget / both / none -------------- #
    def marker_snapshot(count, *, truncated=False):
        nodes = [_node(20000 + i, "button", f"按钮{i}", (i % 40, i // 40,
                                                       i % 40 + 3, i // 40 + 3),
                       1, patterns=["invoke"]) for i in range(count)]
        return {"hwnd": 20, "title": "markers", "rect": [0, 0, 200, 200],
                "total": count, "shown": count, "nodes": nodes,
                "truncated": truncated}

    full_stats: dict = {}
    full_text = compact.format_snapshot(marker_snapshot(3), stats=full_stats)
    check("full snapshot has no cut token",
          "cut=" not in full_text and full_stats["hidden"] == 0
          and not full_stats["tree_cut"], full_text.splitlines()[1])
    tree_stats: dict = {}
    tree_text = compact.format_snapshot(marker_snapshot(3, truncated=True),
                                        stats=tree_stats)
    check("tree truncation -> cut=tree",
          " cut=tree" in tree_text and "budget(" not in tree_text
          and tree_stats["tree_cut"] and not tree_stats["budget_cropped"],
          tree_text.splitlines()[1])
    budget_stats: dict = {}
    budget_text = compact.format_snapshot(marker_snapshot(400), max_chars=700,
                                          stats=budget_stats)
    expected_marker = f"…(+{budget_stats['hidden']} hidden)"
    check("budget crop -> cut=budget(+N hidden)",
          "cut=budget(+" in budget_text and expected_marker in budget_text,
          budget_text.splitlines()[1])
    both_stats: dict = {}
    both_text = compact.format_snapshot(marker_snapshot(400, truncated=True),
                                        max_chars=700, stats=both_stats)
    check("tree+budget -> cut=tree,budget(+N)",
          bool(re.search(r"cut=tree,budget\(\+\d+ hidden\)", both_text)),
          both_text.splitlines()[1])
    check("cut marker count equals dropped lines",
          both_stats["hidden"] == 400 - both_stats["rendered_lines"],
          f"hidden={both_stats['hidden']} rendered={both_stats['rendered_lines']}")

    # -- offscreen priority (B3 fix): kept, but ranked after visible ---------- #
    visible = [_node(30000 + i, "button", f"可见{i}", (i * 5, 0, i * 5 + 4, 4), 1,
                     patterns=["invoke"]) for i in range(20)]
    hidden_off = [_node(40000 + i, "button", f"离屏{i}", (i * 5, 10, i * 5 + 4, 14),
                        1, patterns=["invoke"], offscreen=True)
                  for i in range(40)]
    offscreen_snap = {"hwnd": 21, "title": "offscreen",
                      "rect": [0, 0, 200, 100], "total": 60, "shown": 60,
                      "nodes": visible + hidden_off}
    ample_stats: dict = {}
    ample_text = compact.format_snapshot(offscreen_snap, stats=ample_stats)
    check("offscreen interactive kept when budget is ample",
          "离屏0" in ample_text and "~" in ample_text
          and not ample_stats["budget_cropped"])
    tight_stats: dict = {}
    tight_text = compact.format_snapshot(offscreen_snap, max_chars=900,
                                         stats=tight_stats)
    visible_kept = sum(1 for line in tight_text.splitlines()
                       if "可见" in line)
    offscreen_kept = sum(1 for line in tight_text.splitlines()
                         if "离屏" in line and "~" in line)
    check("budget pressure keeps every visible interactive element",
          visible_kept == 20, f"{visible_kept}/20")
    check("budget pressure drops offscreen interactive first",
          tight_stats["budget_cropped"] and offscreen_kept < 40
          and tight_stats["hidden"] == 40 - offscreen_kept,
          f"kept={offscreen_kept} hidden={tight_stats['hidden']}")
    check("tight crop marker count matches dropped offscreen",
          f"…(+{tight_stats['hidden']} hidden)" in tight_text
          and "cut=budget(+" in tight_text)

    kids = {"hwnd": 11, "title": "t", "rect": [0, 0, 100, 100],
            "nodes": [_node(1, "pane", "容器", (0, 0, 50, 50), 0, kids=3)]}
    check("truncated subtree reports kids=N",
          "kids=3" in compact.format_snapshot(kids))

    windows = [{"hwnd": 1000 + i, "proc": "p", "title": f"窗口{i}",
                "rect": [0, i, 100, i + 50], "focused": i == 0}
               for i in range(20)]
    desk = compact.format_windows(windows, limit=12, elapsed_ms=9)
    check("desktop lists at most 12 windows", desk.count("窗口") == 12)
    check("desktop reports hidden windows", "more windows" in desk)

    check("cap_text enforces hard limit",
          len(compact.cap_text("x" * 20000)) == compact.MAX_TEXT)


# --------------------------------------------------------------------------- #
# 3. in-process coordinate mapping + screenshot degradation
# --------------------------------------------------------------------------- #
def test_coordinates_inprocess():
    section("coordinate mapping / screenshot degradation (in-process)")
    import server as server_mod
    import mock_bridge

    mock_bridge.reset()
    srv = server_mod.SuperCuServer(force_mock=True)
    try:
        srv.last_shot = {"origin": [100, 80], "img_w": 1728, "img_h": 1080,
                         "screen_w": 2560, "screen_h": 1600, "hwnd": 1002}
        check("@n:0.5,0.5 -> physical point",
              srv._to_point_spec("@n:0.5,0.5") == "@1380,880",
              srv._to_point_spec("@n:0.5,0.5"))
        check("@img:864,540 -> same physical point",
              srv._to_point_spec("@img:864,540") == "@1380,880",
              srv._to_point_spec("@img:864,540"))
        check("@x,y passes through untouched",
              srv._to_point_spec("@123,456") == "@123,456")
        check("#id passes through untouched",
              srv._to_point_spec("#8712") == "#8712")
        for bad in ("@n:1.5,0.2", "@n:abc", "@img:x,y"):
            try:
                srv._to_point_spec(bad)
                raised = False
            except server_mod.ToolError:
                raised = True
            check(f"invalid coordinate {bad} -> ToolError", raised)

        srv.last_shot = None
        try:
            srv._to_point_spec("@n:0.1,0.1")
            raised = False
        except server_mod.ToolError as exc:
            raised = "@n" in str(exc)
        check("no screenshot -> actionable ToolError", raised)

        # screenshot failure degrades to text-only, never fails cu_show
        def boom(*args, **kwargs):
            raise RuntimeError("窗口已关闭，无法截图")

        srv.backend.bridge.screenshot = boom
        degraded = srv.cu_show({"window": "#1001", "screenshot": True})
        blocks = degraded.get("content") or []
        check("screenshot failure returns text-only result",
              len([b for b in blocks if b.get("type") == "image"]) == 0
              and not degraded.get("isError"))
        check("screenshot failure explained in text",
              "截图失败" in (blocks[0].get("text") or ""))

        # normalized coordinate conversion after a real (mock) screenshot
        srv2 = server_mod.SuperCuServer(force_mock=True)
        shown = srv2.cu_show({"window": "#1002", "screenshot": True})
        shot_text = (shown.get("content") or [{}])[0].get("text", "")
        match = re.search(r"img (\d+)x(\d+) \(screen (\d+)x(\d+)\)",
                          shot_text)
        check("shot meta exposes both image and screen size", match is not None,
              shot_text.splitlines()[-1][:90])
        if match and srv2.last_shot:
            meta = srv2.last_shot
            spec = srv2._to_point_spec("@n:0.5,0.5")
            x, y = [int(v) for v in spec[1:].split(",")]
            in_screen = (meta["origin"][0] <= x <= meta["origin"][0] + meta["screen_w"]
                         and meta["origin"][1] <= y <= meta["origin"][1] + meta["screen_h"])
            check("normalized point lands inside the shot rect", in_screen, spec)
    finally:
        mock_bridge.reset()


# --------------------------------------------------------------------------- #
# 3b. cu_do inline observe: budget + neutral degradation (P1-1 / P1-2)
# --------------------------------------------------------------------------- #
def test_observe_budget_inprocess():
    section("cu_do inline observe budget / degradation (in-process)")
    import server as server_mod
    import mock_bridge

    mock_bridge.reset()
    try:
        srv = server_mod.SuperCuServer(force_mock=True)
        result = srv.cu_do({"steps": [
            {"op": "click", "target": "button:元素 0 的标题",
             "window": "#1007"}], "observe": "state"})
        text = (result.get("content") or [{}])[0].get("text", "")
        check("P1-1 state block bounded", len(text) <= 3600,
              f"{len(text)} chars / {len(text.splitlines())} lines")
        check("P1-1 state block uses cu_show header",
              "obs " in text and "win #1007" in text)

        srv2 = server_mod.SuperCuServer(force_mock=True)
        r2 = srv2.cu_do({"steps": [
            {"op": "focus", "target": "button:元素 0 的标题",
             "window": "#1007"}]})
        t2 = (r2.get("content") or [{}])[0].get("text", "")
        check("P1-1 no state when the step reports no change",
              "win #" not in t2, t2.splitlines()[0][:80] if t2 else "")

        # P1-2: observer failure must be one neutral line, never 失败/traceback
        srv3 = server_mod.SuperCuServer(force_mock=True)

        def boom(*args, **kwargs):
            raise ValueError("invalid hwnd: 48040182")

        srv3._snapshot = boom
        r3 = srv3.cu_do({"steps": [{"op": "launch", "app": "notepad"}],
                         "observe": "state"})
        t3 = (r3.get("content") or [{}])[0].get("text", "")
        check("P1-2 observe exception -> 'obs skipped: <reason>'",
              "obs skipped: invalid hwnd: 48040182" in t3,
              t3.splitlines()[-1][:90])
        check("P1-2 no traceback / no 失败 wording",
              "Traceback" not in t3 and "obs 失败" not in t3
              and "ValueError" not in t3, t3.splitlines()[-1][:90])

        # task-5: actionable advice for the four new error categories
        advice_cases = {
            "disabled": ("element #12 is disabled", "换"),
            "clipboard_busy": ("clipboard_busy: OpenClipboard failed", "type"),
            "unknown_key": ("unknown_key: 'ctrl+foo'", "键名"),
            "pattern_unavailable": ("pattern_unavailable: ValuePattern", "Pattern"),
        }
        for label, (detail, needle) in advice_cases.items():
            advice = server_mod._advice_for(detail)
            check(f"advice for {label} is actionable", needle in advice, advice[:80])

        # paste must be rejected at protocol level without text/value
        try:
            server_mod.SuperCuServer(force_mock=True).cu_do(
                {"steps": [{"op": "paste", "window": "#1001"}]})
            raised_code = None
        except server_mod.RpcError as exc:
            raised_code = exc.code
        check("paste without text -> RpcError -32602",
              raised_code == -32602, str(raised_code))
    finally:
        mock_bridge.reset()


# --------------------------------------------------------------------------- #
# 4. real bridge regression (runs automatically once T1 lands)
# --------------------------------------------------------------------------- #
def test_real():
    section("real bridge integration")
    session = Session(args=())
    launched_pids_before = _notepad_pids()
    try:
        init = session.send("initialize", {"protocolVersion": "2025-06-18"})
        check("real: initialize ok", (init.get("result") or {}).get(
            "protocolVersion") == "2025-06-18")

        # warm-up call (first UIA snapshot pays COM init), then measure steady state
        session.call("cu_show", {})
        started = time.perf_counter()
        show = session.call("cu_show", {})
        wall = int((time.perf_counter() - started) * 1000)
        text = text_of(show)
        check("real: cu_show foreground window", not is_error(show) and "win #" in text,
              text.splitlines()[0][:90] if text else str(show)[:200])
        if not is_error(show):
            chars = len(text)
            size = len(text.encode("utf-8"))
            lines = len(text.splitlines())
            print(f"     cu_show: {wall}ms wall, {obs_ms(text)}ms server, "
                  f"{chars} chars / {size} bytes / {lines} lines")
            check("real: cu_show < 400ms", wall < 400, f"{wall}ms")
            check("real: cu_show <= 3000 chars", chars <= CONTROL_LIMIT,
                  f"{chars} chars")
            check("real: cu_show <= 110 lines", lines <= 110, f"{lines} lines")
            check("real: text cap holds", chars <= MUCH, f"{chars} chars")

        # id reuse across two snapshots (CONTRACT §7.4)
        show2 = session.call("cu_show", {})
        text2 = text_of(show2)
        ids1 = set(re.findall(r"\[(\d+)\]", text))
        ids2 = set(re.findall(r"\[(\d+)\]", text2))
        if ids1 and ids2:
            reuse = len(ids1 & ids2) / float(len(ids1))
            print(f"     id reuse: {reuse*100:.1f}% ({len(ids1)} ids)")
            check("real: id reuse > 90%", reuse > 0.9, f"{reuse*100:.1f}%")

        # Notepad batch: launch -> write -> expect -> close
        desk_before = set(re.findall(r"#(\d+)",
                                    text_of(session.call("cu_show",
                                                         {"scope": "desktop"}))))
        launch = session.call("cu_do", {"steps": [
            {"op": "launch", "app": "notepad", "timeout_ms": 8000}]})
        launch_text = text_of(launch)
        check("real: launch notepad", not is_error(launch),
              launch_text.splitlines()[0][:90] if launch_text else "")
        launch_match = re.search(r"#(\d+)", launch_text.splitlines()[0]
                                 if launch_text else "")
        note_hwnd = launch_match.group(1) if launch_match else None
        if note_hwnd is None:
            # fallback when the bridge does not report the new hwnd
            desk_after = text_of(session.call("cu_show", {"scope": "desktop"}))
            for line in desk_after.splitlines():
                match = re.match(r"#(\d+)\s+(\S+)", line)
                if not match or match.group(1) in desk_before:
                    continue
                if "notepad" in line.lower() or "记事本" in line:
                    note_hwnd = match.group(1)
                    break
        check("real: launch reports the new #hwnd", note_hwnd is not None,
              launch_text.splitlines()[0][:90] if launch_text else "")

        edit_id = None
        target_spec = f"#{note_hwnd}" if note_hwnd else "notepad"
        note_text = text_of(session.call("cu_show", {"window": target_spec}))
        if note_hwnd:
            check("real: #hwnd resolves to the launched window",
                  note_text.startswith(f"win #{note_hwnd}"),
                  note_text.splitlines()[0][:90] if note_text else "")
        for ct in ("document", "edit"):
            edit_id = find_id(note_text, ct, "文本编辑器")
            if edit_id:
                break
        if edit_id is None:
            match = re.search(r"\[(\d+)\] (edit|document)", note_text)
            edit_id = int(match.group(1)) if match else None
        check("real: notepad exposes a text element", edit_id is not None,
              note_text.splitlines()[0][:90] if note_text else "")

        safe_target = bool(note_hwnd) and note_text.startswith(
            f"win #{note_hwnd}")
        if edit_id and safe_target:
            started = time.perf_counter()
            batch = session.call("cu_do", {"steps": [
                {"op": "set", "target": f"#{edit_id}",
                 "text": "super-cu integration", "expect": "super-cu"},
                {"op": "key", "keys": "alt+f4"},
            ]}, timeout=180)
            wall = int((time.perf_counter() - started) * 1000)
            batch_text = text_of(batch)
            print(f"     cu_do batch: {wall}ms wall\n"
                  + "\n".join("     " + line for line in batch_text.splitlines()[:4]))
            check("real: 2-step batch ok", not is_error(batch),
                  batch_text.splitlines()[0][:90] if batch_text else "")

            # Notepad asks to save; dismiss without saving
            session.call("cu_do", {"steps": [
                {"op": "key", "keys": "alt+n", "timeout_ms": 3000}]})
            time.sleep(0.8)
            verify_spec = f"#{note_hwnd}" if note_hwnd else "notepad"
            still_resp = session.call("cu_show", {"window": verify_spec})
            still = text_of(still_resp)
            closed = is_error(still_resp) or not still.startswith(
                f"win #{note_hwnd}")
            check("real: notepad closed after alt+f4 / alt+n", closed,
                  still.splitlines()[0][:90] if still else "gone")

        # cleanup: close anything we launched, then kill leftovers we own
        try:
            spec = f"#{note_hwnd}" if note_hwnd else "notepad"
            session.call("cu_do", {"steps": [
                {"op": "close", "window": spec, "timeout_ms": 2000}]})
        except Exception:
            pass
    finally:
        stderr = session.close()
        _kill_new_notepads(launched_pids_before)
    check("real: stdout pure", session.bad_lines == [],
          str(session.bad_lines[:2]))
    check("real: logs on stderr", "[supercu]" in stderr)


def _notepad_pids():
    try:
        import psutil
    except Exception:
        return set()
    pids = set()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() in ("notepad.exe",
                                                        "notepad"):
                pids.add(proc.pid)
        except Exception:
            pass
    return pids


def _kill_new_notepads(before):
    now = _notepad_pids()
    for pid in now - before:
        try:
            import psutil
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def main(argv) -> int:
    t0 = time.perf_counter()
    test_compact()
    test_coordinates_inprocess()
    test_observe_budget_inprocess()
    test_protocol()
    real_available = os.path.exists(os.path.join(SRC, "bridge.py"))
    if "--mock-only" not in argv and (real_available or "--real" in argv):
        test_real()
    else:
        section("real bridge integration")
        print("SKIP real bridge regression: src/bridge.py not present yet")
    elapsed = time.perf_counter() - t0
    print(f"\n{'=' * 60}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed in {elapsed:.1f}s")
    for name, detail in FAILED:
        print(f"  FAILED {name}  [{detail}]")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

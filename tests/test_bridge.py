"""T1 self-test -- run: python tests/test_bridge.py

Covers CONTRACT.md §3.4: window list, DPI/space proof, snapshot + stable ids,
real actions (setvalue / type / key / invoke / close) on Notepad, ambiguity
refusal, screenshots and wait().  Every GUI action goes through Bridge.act(),
which takes gui_lock.gui_locked() internally -- test 6 proves that with a spy.

Only Notepad is opened by this test and it is closed at the end.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import dpi  # noqa: E402  (Lead-owned, must run before any UIA/win32/PIL use)
import windows as W  # noqa: E402
import bridge as B  # noqa: E402
from bridge import AmbiguousError, Bridge, BridgeError  # noqa: E402
from gui_lock import GuiBusy, gui_locked  # noqa: E402

TEXT = "super-cu bridge test 1234"
TMP_DIR = Path(tempfile.gettempdir())
SAVE_PATH = TMP_DIR / "supercu_bridge_test.txt"

RESULTS: list[tuple[str, bool, float, str]] = []
STATS: dict[str, float] = {}
ACTIONS: list[tuple[str, bool]] = []
b = Bridge(lock_timeout_s=12.0)  # other agents share the desktop lock
_NOTEPAD: dict = {}


# --------------------------------------------------------------------------- #
def check(name: str, fn) -> bool:
    t0 = time.perf_counter()
    try:
        detail = fn() or ""
        ok = True
    except AssertionError as exc:
        ok, detail = False, f"ASSERT: {exc}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
        if os.environ.get("SUPERCU_DEBUG"):
            traceback.print_exc()
    ms = (time.perf_counter() - t0) * 1000
    RESULTS.append((name, ok, ms, detail))
    print(("PASS" if ok else "FAIL") + f"  {name:<28} {ms:7.1f}ms  {detail}")
    return ok


def hwnd_alive(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd)))


# --------------------------------------------------------------------------- #
# 0. environment
# --------------------------------------------------------------------------- #
def t_dpi() -> str:
    state = dpi.ensure_dpi_aware()
    sw, sh = W.screen_size()
    assert state.get("ok"), f"DPI awareness 设置失败: {state}"
    assert state.get("awareness") == 2, f"awareness={state.get('awareness')} 应为 2 (per-monitor)"
    assert (sw, sh) == tuple(state.get("screen") or (0, 0)), (
        f"GetSystemMetrics={sw}x{sh} 与 dpi 状态 {state.get('screen')} 不一致"
    )
    return (
        f"awareness=2 method={state.get('method')} "
        f"screen={sw}x{sh} virtual={state.get('virtual_screen')}"
    )


def t_space() -> str:
    st = b.space_check()
    sw, sh = W.screen_size()
    phys = st["physical"]
    assert st["ok"], f"空间不一致: {st}"
    assert abs(phys[0] - sw) <= 1 and abs(phys[1] - sh) <= 1, f"metrics={sw}x{sh} vs DC={phys}"
    return f"UIA/win32 == ImageGrab == {sw}x{sh} physical"


# --------------------------------------------------------------------------- #
# 1. window list
# --------------------------------------------------------------------------- #
def t_list_windows() -> str:
    t0 = time.perf_counter()
    wins = W.list_windows()
    ms = (time.perf_counter() - t0) * 1000
    STATS["list_windows_ms"] = ms
    fg = W.foreground_hwnd()
    assert wins, "list_windows 返回空"
    assert len({w["hwnd"] for w in wins}) == len(wins), "hwnd 重复"
    req = {"hwnd", "pid", "proc", "title", "class", "rect", "visible", "minimized", "focused", "z"}
    missing = req - set(wins[0])
    assert not missing, f"字段缺失: {missing}"
    focused = [w for w in wins if w["focused"]]
    assert len(focused) <= 1, f"focused 数量={len(focused)}，最多 1"
    # 服务类窗口（如 GameInputSvc）可能抢到前台却是不可见的 —— 只有可见前台才要求入列
    fg_visible = bool(fg) and W.is_visible(fg)
    if fg_visible:
        assert any(w["hwnd"] == fg for w in wins), f"可见前台窗口 #{fg} 不在列表中"
        assert len(focused) == 1, f"可见前台存在但 focused={len(focused)}"
    else:
        assert not focused, "不可见前台却标记了 focused"
    hits = W.find_windows("msedge", limit=3)
    assert isinstance(hits, list)
    head = focused[0] if focused else None
    return (
        f"{len(wins)} windows enum={ms:.1f}ms fg=#{fg}("
        f"{'visible' if fg_visible else 'invisible'}, {W.proc_name(W.get_pid(fg)) if fg else '-'}) "
        f"focused={len(focused)}{' #%d %s' % (head['hwnd'], head['proc']) if head else ''} "
        f"find(msedge)={len(hits)}"
    )


# --------------------------------------------------------------------------- #
# 2. snapshot + stable ids (Notepad)
# --------------------------------------------------------------------------- #
def t_snapshot() -> str:
    s = b.snapshot(_NOTEPAD["hwnd"], view="controls")
    assert s["shown"] > 0, "shown=0"
    assert s["total"] > 0, "total=0"
    assert s["space"] == "physical"
    cts = {n["ct"] for n in s["nodes"]}
    assert "edit" in cts or "document" in cts, f"没有可编辑内容节点: {cts}"
    ids = [n["id"] for n in s["nodes"]]
    assert len(ids) == len(set(ids)), "节点 id 有重复"
    node = s["nodes"][0]
    for key in ("id", "ct", "name", "val", "aid", "cls", "rect", "enabled", "offscreen",
                "focused", "patterns", "kids", "depth", "path"):
        assert key in node, f"node 缺字段 {key}"
    s2 = b.snapshot(_NOTEPAD["hwnd"], view="controls")
    a = {n["id"] for n in s["nodes"]}
    c = {n["id"] for n in s2["nodes"]}
    reuse = len(a & c) / max(1, len(a))
    STATS["notepad_snapshot_ms"] = float(s["elapsed_ms"])
    STATS["notepad_total"] = float(s["total"])
    STATS["notepad_shown"] = float(s["shown"])
    STATS["reuse_notepad"] = reuse
    assert reuse > 0.90, f"id 复用率 {reuse:.0%} <= 90%"
    multi = [n for n in s["nodes"] if n["depth"] > 0]
    assert multi, "所有节点 depth=0，层级信息丢失"
    return (
        f"elapsed={s['elapsed_ms']}/{s2['elapsed_ms']}ms total={s['total']} shown={s['shown']} "
        f"reuse={reuse:.0%} cts={sorted(cts)}"
    )


def t_snapshot_text_view() -> str:
    s = b.snapshot(_NOTEPAD["hwnd"], view="text", max_nodes=60)
    assert s["shown"] > 0
    return f"view=text total={s['total']} shown={s['shown']} {s['elapsed_ms']}ms"


# --------------------------------------------------------------------------- #
# 3. actions: setvalue
# --------------------------------------------------------------------------- #
def _pick_edit_node() -> dict:
    s = b.snapshot(_NOTEPAD["hwnd"], view="controls")
    edits = [n for n in s["nodes"] if n["ct"] in ("edit", "document")]
    assert edits, "找不到可编辑元素"
    return edits[-1]


def t_setvalue() -> str:
    node = _pick_edit_node()
    r: dict = {}
    last_val: str | None = None
    # 桌面可能被别人同时操作（外部键入会污染文档），回读不一致就重试一次
    for attempt in range(3):
        r = b.act(_NOTEPAD["hwnd"], "setvalue", target=f"#{node['id']}", value=TEXT)
        if not r["ok"]:
            if attempt < 2:
                time.sleep(0.3)
                continue
            raise AssertionError(r["detail"])
        time.sleep(0.2)
        s = b.snapshot(_NOTEPAD["hwnd"], view="text", max_nodes=20)
        doc_text = "".join((n.get("val") or "") for n in s["nodes"] if n["ct"] == "document")
        if TEXT in doc_text:
            break
        last_val = doc_text
        time.sleep(0.3)
    else:
        raise AssertionError(f"UIA TextPattern 回读内容不符（重试 3 次）: {str(last_val)[:120]!r}")
    ACTIONS.append(("setvalue", bool(r["ok"])))
    assert r["action"] == "setvalue" and r["via"] in ("setvalue", "paste"), r
    return f"via={r['via']} {r['ms']}ms textpattern_readback=ok attempts={attempt + 1}"


# --------------------------------------------------------------------------- #
# 4. key -> save dialog -> type -> invoke 保存 -> file on disk
# --------------------------------------------------------------------------- #
def find_save_dialog(timeout: float = 5.0) -> dict | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for q in ("另存为", "Save As"):
            hits = W.find_windows(q, limit=1)
            if hits:
                return hits[0]
        time.sleep(0.05)
    return None


def act_retry(hwnd: int, action: str, tries: int = 2, **kw) -> dict:
    """act() with a retry when another agent holds the shared desktop lock."""
    r: dict = {}
    for i in range(tries):
        r = b.act(hwnd, action, **kw)
        if r.get("ok") or "占用" not in (r.get("detail") or ""):
            return r
        time.sleep(0.8)
    return r


def t_save_chain() -> str:
    """setvalue → Ctrl+S → 磁盘内容校验（文件已有路径，无需另存为对话框）。"""
    SAVE_TEXT = TEXT + " saved"  # 与 t_setvalue 不同，确保文档变脏、Ctrl+S 真的写盘
    SAVE_PATH.write_text("placeholder", encoding="utf-8")
    doc = next(
        (n for n in b.snapshot(_NOTEPAD["hwnd"], view="controls")["nodes"] if n["ct"] == "document"),
        None,
    )
    assert doc, "找不到 document 节点"
    rf = act_retry(_NOTEPAD["hwnd"], "focus", target=f"#{doc['id']}")
    ACTIONS.append(("focus", bool(rf["ok"])))
    assert rf["ok"], rf["detail"]
    r0 = act_retry(_NOTEPAD["hwnd"], "setvalue", target=f"#{doc['id']}", value=SAVE_TEXT)
    ACTIONS.append(("setvalue", bool(r0["ok"])))
    assert r0["ok"], r0["detail"]
    # ValuePattern 改内容不一定把文档标脏；补一个真实按键确保 Ctrl+S 会写盘
    rn = act_retry(_NOTEPAD["hwnd"], "type", text=" ")
    ACTIONS.append(("type", bool(rn["ok"])))
    r = act_retry(_NOTEPAD["hwnd"], "key", keys="ctrl+s")
    ACTIONS.append(("key", bool(r["ok"])))
    assert r["ok"], r["detail"]
    content = ""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            content = SAVE_PATH.read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""
        if SAVE_TEXT in content:
            break
        time.sleep(0.1)
    assert SAVE_TEXT in content, f"Ctrl+S 后磁盘内容不符: {content[:120]!r}"
    return (
        f"focus={rf['ms']}ms setvalue={r0['ms']}ms({r0['via']}) key={r['ms']}ms "
        f"file={SAVE_PATH.name} {len(content)}B content_ok"
    )


def t_lock_wrapped() -> str:
    """act() must take gui_lock.gui_locked() for focus/keyboard/mouse actions."""
    calls: list[str] = []
    orig = B.gui_locked

    @contextlib.contextmanager
    def spy(*a, **kw):
        kw2 = dict(kw)
        kw2["timeout_s"] = 0  # never block the test on another agent's hold
        try:
            with orig(*a, **kw2):
                calls.append("acquired")
                yield
        except GuiBusy:
            calls.append("busy-skip")
            yield

    B.gui_locked = spy
    try:
        r = b.act(_NOTEPAD["hwnd"], "activate")
    finally:
        B.gui_locked = orig
    assert calls, "act() 没有进入 gui_locked()（会和其他 agent 抢桌面）"
    assert r["ok"], r["detail"]
    return f"act() entered gui_locked({calls[0]}) activate={r['ms']}ms"


# --------------------------------------------------------------------------- #
# 5. ambiguity refusal
# --------------------------------------------------------------------------- #
def t_class_ambiguous() -> str:
    fake = [(None, 0, (), "ButtonControl", "保存", 0), (None, 0, (), "ButtonControl", "保存", 0)]
    n_cands = 0
    try:
        b._pick(fake, "button:保存", "保存")
        raise AssertionError("_pick 没有对两个同名按钮报歧义")
    except AmbiguousError as exc:
        n_cands = len(exc.candidates)
    # real-world check (read-only): look for duplicate ct+name in visible windows
    real = None
    for w in W.list_windows()[:6]:
        if w["minimized"] or not w["title"]:
            continue
        if "deepseek" in w["proc"].lower():
            continue  # never poke the harness window
        try:
            s = b.snapshot(w["hwnd"], view="controls", max_nodes=160)
        except BridgeError:
            continue
        seen: dict[tuple, int] = {}
        for node in s["nodes"]:
            key = (node["ct"], node["name"])
            if node["name"] and node["ct"] not in ("text", "pane", "window"):
                seen[key] = seen.get(key, 0) + 1
        dup = next(((k, v) for k, v in seen.items() if v > 1), None)
        if dup:
            real = (w, dup[0], dup[1])
            break
    if real is None:
        return f"synthetic_ok candidates={n_cands}; 扫描窗口无同名同类型元素（真实场景跳过并说明）"
    w, (ct, name), cnt = real
    try:
        b.resolve(f"{ct}:{name}", w["hwnd"])
        raise AssertionError(f"真实场景 {ct}:{name} x{cnt} 未报歧义")
    except AmbiguousError as exc:
        return (
            f"synthetic_ok candidates={n_cands}; real_ok window#{w['hwnd']} "
            f"{ct}:{name!r} x{cnt} -> {len(exc.candidates)} cands"
        )


# --------------------------------------------------------------------------- #
# 6. screenshots
# --------------------------------------------------------------------------- #
def t_screenshot() -> str:
    sw, sh = W.screen_size()
    full = b.screenshot()
    ms_full = full["ms"]
    assert abs(full["img_w"] * full["scale"] - sw) <= 1, (
        f"img_w*scale={full['img_w'] * full['scale']:.2f} != screen_w={sw}"
    )
    assert abs(full["img_w"] * full["scale"] - full["screen_w"]) <= 1
    assert full["mime"] == "image/jpeg" and full["jpeg_bytes"][:2] == b"\xff\xd8"
    crop = b.screenshot(_NOTEPAD["hwnd"])
    assert abs(crop["img_w"] * crop["scale"] - crop["screen_w"]) <= 1, (
        f"crop 不自洽 {crop['img_w']}*{crop['scale']} != {crop['screen_w']}"
    )
    assert crop["screen_w"] <= full["screen_w"]
    # @n: / @img: round-trip
    b._last_shot = full
    x, y = b._point_from_spec("@n:0.5,0.5")
    assert abs(x - sw / 2) <= 2 and abs(y - sh / 2) <= 2, f"@n 换算错误 {(x, y)} vs {(sw/2, sh/2)}"
    xi, yi = b._point_from_spec(f"@img:{full['img_w'] // 2},{full['img_h'] // 2}")
    assert abs(xi - sw / 2) <= 2, f"@img 换算错误 {xi}"
    b._last_shot = crop
    STATS["shot_full_ms"] = float(ms_full)
    STATS["shot_crop_ms"] = float(crop["ms"])
    STATS["shot_full_bytes"] = float(len(full["jpeg_bytes"]))
    return (
        f"full {full['img_w']}x{full['img_h']} scale={full['scale']:.6f} "
        f"screen={full['screen_w']}x{full['screen_h']} {ms_full}ms {len(full['jpeg_bytes']) // 1024}KB | "
        f"crop {crop['img_w']}x{crop['img_h']} {crop['ms']}ms origin={crop['origin']}"
    )


# --------------------------------------------------------------------------- #
# 7. wait
# --------------------------------------------------------------------------- #
def t_wait() -> str:
    title = W.get_title(_NOTEPAD["hwnd"])
    needle = "Notepad" if "Notepad" in title else title.split("-")[-1].strip()
    assert needle, f"窗口标题为空: {title!r}"
    w1 = b.wait("window", text=needle, timeout_ms=2500)
    assert w1["ok"], f"{w1['detail']} (needle={needle!r})"
    w2 = b.wait("element", target="document:*", hwnd=_NOTEPAD["hwnd"], timeout_ms=2500)
    assert w2["ok"], w2["detail"]
    w3 = b.wait("text", text=needle, hwnd=_NOTEPAD["hwnd"], timeout_ms=2500)
    assert w3["ok"], w3["detail"]
    return f"window={w1['ms']}ms element={w2['ms']}ms text={w3['ms']}ms needle={needle!r}"


# --------------------------------------------------------------------------- #
# 8. Edge snapshot (read-only, performance reference)
# --------------------------------------------------------------------------- #
def t_edge_snapshot() -> str:
    cands = [w for w in W.find_windows("msedge", limit=8)
             if not w["minimized"] and w["visible"] and (w["rect"][2] - w["rect"][0]) > 900]
    if not cands:
        return "SKIP: 没有可见的 Edge 窗口（只读快照，不做任何点击）"
    win = cands[0]
    times = []
    reuse_rates = []
    prev = None
    for _ in range(3):
        s = b.snapshot(win["hwnd"], view="controls")
        times.append(s["elapsed_ms"])
        ids = {n["id"] for n in s["nodes"]}
        if prev:
            reuse_rates.append(len(prev & ids) / max(1, len(prev)))
        prev = ids
        last = s
    STATS["edge_snapshot_p50"] = float(sorted(times)[1])
    STATS["edge_total"] = float(last["total"])
    STATS["edge_shown"] = float(last["shown"])
    if reuse_rates:
        STATS["reuse_edge"] = sum(reuse_rates) / len(reuse_rates)
    return (
        f"win#{win['hwnd']} {win['proc']} times={times} total={last['total']} shown={last['shown']} "
        f"truncated={last['truncated']} reuse={STATS.get('reuse_edge', 0):.0%}"
    )


# --------------------------------------------------------------------------- #
# 9. close Notepad
# --------------------------------------------------------------------------- #
def kill_window_pid(hwnd: int) -> None:
    try:
        pid = W.window_info(hwnd)["pid"]
    except Exception:
        pid = _NOTEPAD.get("pid")
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def t_close() -> str:
    hwnd = _NOTEPAD["hwnd"]
    r = act_retry(hwnd, "close")
    deadline = time.monotonic() + 3
    while hwnd_alive(hwnd) and time.monotonic() < deadline:
        time.sleep(0.05)
    forced = False
    if hwnd_alive(hwnd):
        # Honest ok=False (a dialog is in the way) or another agent holding the
        # desktop lock: cleanup still has to happen.
        forced = True
        kill_window_pid(hwnd)
        deadline = time.monotonic() + 4
        while hwnd_alive(hwnd) and time.monotonic() < deadline:
            time.sleep(0.05)
    assert not hwnd_alive(hwnd), "Notepad 没有关闭"
    ACTIONS.append(("close", bool(r["ok"])))
    note = ""
    if forced:
        note = "；act 如实报告未关闭(见 detail)，测试用 taskkill 兜底"
    return f"ok={r['ok']} via={r['via']} {r['ms']}ms detail={r['detail']!r}{note}"


# --------------------------------------------------------------------------- #
# setup / teardown
# --------------------------------------------------------------------------- #
def launch_notepad() -> dict:
    # 打开一个**已有路径**的文件：文档自带路径，Ctrl+S 不会弹另存为对话框，
    # 也让 launch 带上参数（顺带验证 launch 的窗口定位）。
    SAVE_PATH.write_text("placeholder", encoding="utf-8")
    before = {w["hwnd"] for w in W.list_windows()}
    proc = subprocess.Popen(["notepad.exe", str(SAVE_PATH)])
    win = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for w in W.list_windows():
            if w["hwnd"] in before:
                continue
            if w["proc"].lower() != "notepad" or not w["visible"]:
                continue
            if SAVE_PATH.name in (w["title"] or "") or not win:
                win = w
            if SAVE_PATH.name in (w["title"] or ""):
                break
        if win and SAVE_PATH.name in (win["title"] or ""):
            break
        time.sleep(0.1)
    assert win, "10s 内没等到 Notepad 窗口"
    # wait until UIA exposes an editable node
    t0 = time.monotonic() + 6
    while time.monotonic() < t0:
        try:
            s = b.snapshot(win["hwnd"], view="controls", max_nodes=40)
            if any(n["ct"] in ("edit", "document") for n in s["nodes"]):
                break
        except BridgeError:
            pass
        time.sleep(0.1)
    try:
        import psutil

        pid = W.window_info(win["hwnd"])["pid"]  # new Notepad may reuse a process
        proc_obj = psutil.Process(pid)
    except Exception:
        pid = proc.pid
        proc_obj = None
    return {"hwnd": win["hwnd"], "pid": pid, "proc": proc, "psutil_proc": proc_obj}


# --------------------------------------------------------------------------- #
# 11. UWP (计算器): launch 严格定位 / 树重建语义重定位
# --------------------------------------------------------------------------- #
_CALC: dict = {}


def _calc_hwnd() -> int:
    return int(_CALC.get("hwnd") or 0)


def _calc_nodes() -> list[dict]:
    return b.snapshot(_calc_hwnd(), view="controls", max_nodes=120)["nodes"]


def t_uwp_launch() -> str:
    """P0-1: launch must return the app's OWN window (or honest ok:false)."""
    fg = W.foreground_hwnd()
    prev = {w["hwnd"] for w in W.list_windows()}
    r = b.act(fg, "launch", text="calc.exe")
    detail = r["detail"]
    assert "match=" in detail, f"detail 缺少 match=: {detail}"
    note = ""
    if r["ok"]:
        h = int(r.get("hwnd") or 0)
    elif "match=existing" in detail:
        m = re.search(r"#(\d+)", detail)
        h = int(m.group(1)) if m else 0
        note = " (复用已有窗口，ok:false 符合契约)"
    else:
        raise AssertionError(f"launch 失败且没有真实窗口: {detail}")
    assert h, f"launch 没给出 hwnd: {detail}"
    try:
        _CALC.update(W.window_info(h))
    except Exception:
        pass
    assert h != fg, f"launch 返回了调用方窗口 #{h}（前台 #{fg}）—— P0-1 回归"
    info = W.window_info(h)
    low = (info["class"] + info["proc"]).lower()
    assert ("applicationframe" in low) or ("calc" in low) or ("windows.ui.core" in low), info
    if r["ok"]:
        assert h not in prev, f"#{h} 在 launch 前就存在，不是新窗口"
    _CALC.update(info)
    return f"#{h} {info['proc']}/{info['class']} {info['title']!r} match={detail.split('match=')[-1]}{note}"


def t_uwp_click() -> str:
    """Pattern click on a UWP button, verified through UIA text."""
    # a freshly launched UWP app needs ~0.5-2s before its XAML tree is ready
    target = None
    nodes: list[dict] = []
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        nodes = _calc_nodes()
        names = {n["name"] for n in nodes if n["ct"] == "button"}
        target = next((x for x in ("七", "7", "Seven") if x in names), None)
        if target:
            break
        time.sleep(0.25)
    assert target, f"计算器按钮名不含 7: {[n['name'] for n in nodes if n['ct'] == 'button'][:20]}"
    btn = next(n for n in nodes if n["ct"] == "button" and n["name"] == target)
    r = b.act(_calc_hwnd(), "invoke", target=f"#{btn['id']}")
    ACTIONS.append(("invoke", bool(r["ok"])))
    assert r["ok"], r["detail"]
    time.sleep(0.35)
    assert any("7" in (n["name"] or "") for n in _calc_nodes()), "点 7 后界面里没出现 7"
    return f"button {target!r} invoke={r['ms']}ms via={r['via']} display_has_7=True"


def t_uwp_relocate() -> str:
    """P0-2: stale id (tree rebuilt) must be relocated semantically."""
    nodes = _calc_nodes()
    btn = next(
        (n for n in nodes if n["ct"] == "button" and n["name"] in ("八", "8", "Eight")), None
    )
    assert btn, f"找不到 8 按钮: {[n['name'] for n in nodes if n['ct'] == 'button'][:20]}"
    eid = btn["id"]
    el = b.by_id(eid)
    assert el is not None, "by_id 拿不到缓存元素"
    # 模拟 UWP 树重建：旧 control / RuntimeId 全失效，只剩语义信息
    el.control = None
    el.rid = ("stale", 0)
    r = b.act(_calc_hwnd(), "invoke", target=f"#{eid}")
    ACTIONS.append(("invoke", bool(r["ok"])))
    assert r["ok"], f"语义重定位失败: {r['detail']}"
    assert "relocated=" in (r["detail"] or ""), f"没有走语义重定位: {r['detail']}"
    time.sleep(0.35)
    assert any("8" in (n["name"] or "") for n in _calc_nodes()), "重定位后点 8 没有生效"
    return f"#{eid} {r['detail']} {r['ms']}ms display_has_8=True"


def t_uwp_close() -> str:
    h = _calc_hwnd()
    if not h or not hwnd_alive(h):
        return "SKIP 计算器窗口已不在"
    r = b.act(h, "close")
    deadline = time.monotonic() + 4
    while hwnd_alive(h) and time.monotonic() < deadline:
        time.sleep(0.05)
    if hwnd_alive(h):
        kill_window_pid(h)
        deadline = time.monotonic() + 3
        while hwnd_alive(h) and time.monotonic() < deadline:
            time.sleep(0.05)
    assert not hwnd_alive(h), "计算器没关掉"
    _CALC.clear()
    return f"ok={r['ok']} {r['ms']}ms detail={r['detail']!r}"


# --------------------------------------------------------------------------- #
# 12. 非 GUI 单测：按键别名 / 禁用检查 / set 回读 / 剪贴板兜底
# --------------------------------------------------------------------------- #
class _FakeValuePattern:
    def __init__(self, store: dict, transform=None):
        self.store = store
        self.transform = transform

    @property
    def Value(self):
        return self.transform(self.store["v"]) if self.transform else self.store["v"]

    def SetValue(self, v):
        self.store["v"] = v


class _FakeCtl:
    def __init__(self, name="保存", enabled=True, value_pat=None, has_invoke=False):
        self.Name = name
        self.ControlTypeName = "ButtonControl"
        self.ClassName = "Button"
        self.AutomationId = ""
        self.IsEnabled = enabled
        self.IsOffscreen = False
        self.HasKeyboardFocus = False
        self._vp = value_pat
        self._has_invoke = has_invoke

    def GetValuePattern(self):
        return self._vp

    def GetInvokePattern(self):
        if not self._has_invoke:
            raise Exception("no invoke pattern")
        pat = self

        class _P:
            def Invoke(self_inner):
                pat.invoked = True

        return _P()

    def GetChildren(self):
        return []

    def SetFocus(self):
        pass

    def GetRuntimeId(self):
        return ["fake", id(self)]


def _fake_el(ctl, name="保存"):
    return B.Element(
        eid=613,
        hwnd=0,
        rid=None,
        control=ctl,
        node={"id": 613, "ct": "button", "name": name},
        ts=time.monotonic(),
    )


def t_key_aliases() -> str:
    cases = {
        "PGDOWN": 0x22,
        "PGDN": 0x22,
        "PageDown": 0x22,
        "page_down": 0x22,
        "{PGDOWN}": 0x22,
        "PGUP": 0x21,
        "PRIOR": 0x21,
        "NEXT": 0x22,
        "ESC": 0x1B,
        "RETURN": 0x0D,
        "Bksp": 0x08,
        "Ins": 0x2D,
        "spacebar": 0x20,
        "prtsc": 0x2C,
        "WIN": 0x5B,
        "F24": 0x87,
    }
    for name, want in cases.items():
        chords = B._parse_keys(name)
        got = B._vk_for(chords[0][1])[0]
        assert got == want, f"{name!r} -> vk 0x{got:02X}, 期望 0x{want:02X}"
    assert B._parse_keys("^s") == [({"ctrl"}, "s")]
    assert B._parse_keys("alt+f4") == [({"alt"}, "f4")]
    assert B._parse_keys("ctrl+shift+t") == [({"ctrl", "shift"}, "t")]
    assert B._parse_keys("{ENTER}") == [(set(), "ENTER")]
    try:
        B._vk_for("definitely_not_a_key")
        raise AssertionError("未知键居然没报错")
    except B.BridgeError as exc:
        msg = str(exc)
        assert "可用按键" in msg and "pgdown" in msg, msg
    return f"{len(cases)} aliases ok; ^s/alt+f4/ctrl+shift+t/{{ENTER}} 不变; 未知键带可用清单"


def t_disabled_and_pattern() -> str:
    try:
        b._require_enabled(_FakeCtl(enabled=False), "'#613 button 保存(S)'", "click")
        raise AssertionError("disabled 元素没有报错")
    except B.BridgeError as exc:
        msg = str(exc)
        assert "禁用" in msg and "保存" in msg, msg
    el = _fake_el(_FakeCtl(enabled=True, has_invoke=False))
    try:
        b._run_action(0, "invoke", el, {"via": "id"}, None, None, None, 0, None)
        raise AssertionError("invoke 无 pattern 居然没报错")
    except B.BridgeError as exc:
        assert "pattern_unavailable" in str(exc), str(exc)
    okctl = _FakeCtl(enabled=True, has_invoke=True)
    via, detail, changed = b._run_action(
        0, "invoke", _fake_el(okctl), {"via": "id"}, None, None, None, 0, None
    )
    assert getattr(okctl, "invoked", False) and via == "invoke", (via, detail)
    try:
        b._run_action(
            0, "click", _fake_el(_FakeCtl(enabled=False)), {"via": "id"}, None, None, None, 0, None
        )
        raise AssertionError("disabled click 没报错")
    except B.BridgeError as exc:
        assert "禁用" in str(exc)
    return "disabled→ok:false(禁用语义) / invoke 无 pattern→pattern_unavailable / 正常 invoke ok"


def t_setvalue_verify() -> str:
    store = {"v": "old"}
    good = _FakeCtl(name="文件名", value_pat=_FakeValuePattern(store))
    via, detail, ok = b._set_value(good, "new value")
    assert ok and b._last_verified is True, (via, detail, b._last_verified)
    assert store["v"] == "new value"
    assert "verified" in detail, detail
    store2 = {"v": "old"}
    bad = _FakeCtl(name="文件名", value_pat=_FakeValuePattern(store2, transform=lambda v: "REJECTED"))
    try:
        b._set_value(bad, "new value")
        raise AssertionError("回读不一致居然没报错")
    except B.BridgeError as exc:
        assert "回读不一致" in str(exc), str(exc)
    return "verified=true；回读被改写 → ok:false 且 detail 带回读值"


def t_clipboard_roundtrip() -> str:
    prev = W.get_clipboard_text()
    payload = "super-cu 剪贴板自检 %d" % int(time.time())
    t0 = time.perf_counter()
    ok = W.set_clipboard_text(payload, attempts=6)
    write_ms = (time.perf_counter() - t0) * 1000
    got = W.get_clipboard_text()
    restored = False
    if prev is not None:
        restored = W.set_clipboard_text(prev, attempts=6)
    STATS["clipboard_write_ms"] = write_ms
    assert ok, "set_clipboard_text 连续 6 次失败（剪贴板被占用？）"
    attempts = getattr(W, "LAST_CLIPBOARD_ATTEMPTS", 0)
    assert got == payload, f"回读不一致: {got!r} != {payload!r}"
    STATS["clipboard_attempts"] = float(attempts)
    orig = W.set_clipboard_text
    W.set_clipboard_text = lambda text, attempts=6: False
    try:
        assert B._clipboard_write_verified("x", attempts=2) is False, "写入失败居然返回 True"
    finally:
        W.set_clipboard_text = orig
    return (
        f"write={write_ms:.0f}ms attempts={attempts} readback_ok restored_prev={restored}; "
        f"失败路径 _clipboard_write_verified=False"
    )


def t_paste_document() -> str:
    """真实粘贴链路：剪贴板 → Ctrl+V → TextPattern 回读校验。"""
    payload = "粘贴校验 PASTE-%d" % int(time.time())
    doc = next(
        (n for n in b.snapshot(_NOTEPAD["hwnd"], view="controls")["nodes"] if n["ct"] == "document"),
        None,
    )
    assert doc, "找不到 document 节点"
    rf = act_retry(_NOTEPAD["hwnd"], "focus", target=f"#{doc['id']}")
    assert rf["ok"], rf["detail"]
    r = act_retry(_NOTEPAD["hwnd"], "paste", text=payload)
    ACTIONS.append(("paste", bool(r["ok"])))
    assert r["ok"], r["detail"]
    time.sleep(0.3)
    s = b.snapshot(_NOTEPAD["hwnd"], view="text", max_nodes=20)
    text = "".join((n.get("val") or "") for n in s["nodes"] if n["ct"] == "document")
    assert payload in text, f"粘贴内容没进文档: via={r['via']} detail={r['detail']!r} doc={text[:80]!r}"
    return (
        f"via={r['via']} {r['ms']}ms clipboard_attempts={getattr(W, 'LAST_CLIPBOARD_ATTEMPTS', 0)} "
        f"doc_readback_ok"
    )


def t_paste_fallback() -> str:
    payload = "这是一段比较长的中文文本，用来验证剪贴板不可用时的 SendKeys 兜底路径，四十字符以上。" * 2
    calls = {"chunks": 0}

    def fake_chunks(text):
        calls["chunks"] += 1
        return len([p for p in B.Bridge.plan_chunks(text) if p is not None])

    orig_focus = b._ensure_focus
    orig_paste = b._paste_via_clipboard
    orig_chunks = b._type_by_chunks
    b._ensure_focus = lambda hwnd: None
    b._paste_via_clipboard = lambda content: False
    b._type_by_chunks = fake_chunks
    try:
        via, detail, ok = b._paste(0, payload)
        assert ok and via == "sendkeys", (via, detail)
        assert "chunks=" in detail, detail
        n = int(detail.split("chunks=")[-1])
        assert calls["chunks"] == 1 and n >= 3, (calls, detail)
        b._type_by_chunks = lambda text: 0
        try:
            b._paste(0, payload)
            raise AssertionError("两条路都失败居然没报 ClipboardBusy")
        except B.ClipboardBusy as exc:
            assert "剪贴板被其他程序占用" in str(exc), str(exc)
    finally:
        b._ensure_focus = orig_focus
        b._paste_via_clipboard = orig_paste
        b._type_by_chunks = orig_chunks
    return f"剪贴板不可用 → via=sendkeys chunks={n}；两条都失败 → ClipboardBusy"


def t_heavy_perf() -> str:
    """重窗口（truncated 路径）快照性能：**按节点数缩放的相对阈值**。

    实测 scaling（同一台机器、controls 视图、max_nodes=100，空载）：
        nodes  ~180  ->  p50 ~110ms      (Notepad 级)
        nodes   281  ->  p50 ~178ms      (bench/stress.py c1 t100)
        nodes   566  ->  p50 ~374ms      (bench/stress.py c1 t1000)
        nodes   658  ->  p50 420-500ms   (超长 DSH 会话窗口，全是工具调用行)
    即 p50 ≈ linear in nodes；硬编码 400ms 会在 ≥650 节点的窗口上误报，所以这里
    用 `max(400, 0.85*nodes + 60)` —— 281→299 / 566→541 / 658→619，
    真回归（同等节点数慢 50% 以上）仍会立刻被抓到。

    结论：≥650 节点的会话窗口 p50 ≈ 0.4-0.5s，仍约为 windows-mcp 基线
    (Snapshot p50 2112ms) 的 1/5。
    """
    cands = [w for w in W.list_windows() if w["visible"] and not w["minimized"]]
    cands.sort(key=lambda w: -(w["rect"][2] - w["rect"][0]) * (w["rect"][3] - w["rect"][1]))
    best = None
    best_total = 0
    for w in cands[:4]:
        try:
            s0 = b.snapshot(w["hwnd"], view="controls", max_nodes=100)
        except BridgeError:
            continue
        if s0["total"] > best_total:
            best_total, best = s0["total"], w
    if best is None or best_total < 120:
        return f"SKIP: 桌面没有重窗口（最重 {best_total} 节点）"
    ts = []
    s = None
    for _ in range(3):
        t = time.perf_counter()
        s = b.snapshot(best["hwnd"], max_nodes=100)
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    p50, p95 = ts[1], ts[-1]
    assert s is not None
    nodes = s["total"]
    budget = max(400.0, 0.85 * nodes + 60.0)
    STATS["heavy_total"] = float(nodes)
    STATS["heavy_p50"] = p50
    STATS["heavy_budget_ms"] = budget
    assert s["truncated"], f"重窗口未触发 bounded DFS（shown={s['shown']}）"
    assert p50 <= budget, (
        f"重窗口 p50 {p50:.0f}ms > 相对阈值 {budget:.0f}ms（nodes={nodes}，"
        f"预算= max(400, 0.85*nodes+60)）"
    )
    return (
        f"win#{best['hwnd']} {best['proc']} total={nodes} shown={s['shown']} "
        f"truncated=True p50={p50:.0f}ms p95={p95:.0f}ms budget={budget:.0f}ms"
    )


def t_drag_geometry() -> str:
    """纯几何单测：拖动必须在 X 与 Y 两个轴上都走完，终点精确等于 to。

    回归 "水平位移丢失/只发 Y" 这类 bug，不需要 GUI。
    """
    moves: list[tuple[int, int]] = []
    buttons: list[str] = []
    orig = (B._move_cursor, B._move_cursor_abs, B._mouse, b._ensure_focus, b._looks_like_caption)
    B._move_cursor = lambda x, y: moves.append((x, y))
    B._move_cursor_abs = lambda x, y: moves.append((int(x), int(y)))
    B._mouse = lambda btn, down: buttons.append(f"{btn}{'D' if down else 'U'}")
    b._ensure_focus = lambda hwnd: None
    b._looks_like_caption = lambda hwnd, x, y: False
    try:
        r = b._drag(0, None, {"via": "point", "point": [100, 100], "node": {}, "candidates": []}, "@260,190")
    finally:
        B._move_cursor, B._move_cursor_abs, B._mouse, b._ensure_focus, b._looks_like_caption = orig
    assert r[0] == "click" and r[2] is True, r
    assert moves and moves[0] == (100, 100), f"起点不对: {moves[:3]}"
    assert moves[-1] == (260, 190), f"终点不对: {moves[-1]} != (260,190)"
    xs = {m[0] for m in moves}
    ys = {m[1] for m in moves}
    assert len(xs) >= 5, f"X 方向只走了 {len(xs)} 个不同值（水平位移丢失）: {moves}"
    assert len(ys) >= 5, f"Y 方向只走了 {len(ys)} 个不同值: {moves}"
    assert buttons == ["leftD", "leftU"], f"按键顺序不对: {buttons}"
    span = max(x for x, _ in moves) - min(x for x, _ in moves)
    assert span == 160, f"水平行程 {span} != 160"
    return f"moves={len(moves)} 个点，X 不同值 {len(xs)}，终点 (260,190) 精确，行程 160px"


def _winforms_target(marker: str = "SUPERCU_TEST_DRAG", timeout_s: float = 20.0) -> dict:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"$f = New-Object System.Windows.Forms.Form; $f.Text = '{marker}'; "
        "$f.Width = 700; $f.Height = 460; $f.StartPosition = 'Manual'; "
        "$f.Left = 400; $f.Top = 300; [System.Windows.Forms.Application]::Run($f)"
    )
    before = {w["hwnd"] for w in W.list_windows()}
    proc = subprocess.Popen(["powershell", "-NoProfile", "-STA", "-Command", ps])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for w in W.list_windows():
            if w["hwnd"] not in before and marker in (w["title"] or ""):
                return {"hwnd": w["hwnd"], "pid": w["pid"], "proc": proc}
        time.sleep(0.15)
    raise AssertionError("WinForms 测试窗口未出现")


def t_drag_window() -> str:
    """真机自测：水平/混合拖动 Δ 必须等于光标位移（±3px），最大化窗口先还原。"""
    win = _winforms_target("SUPERCU_TEST_DRAG")
    h = win["hwnd"]
    try:
        time.sleep(0.7)
        W.activate(h, 200)

        def drag(dx: int, dy: int) -> tuple[tuple[int, int], dict]:
            r0 = W.get_rect(h)
            x, y = r0[0] + 300, r0[1] + 12
            res = b.act(h, "drag", target=f"@{x},{y}", to=f"@{x + dx},{y + dy}")
            time.sleep(0.4)
            r1 = W.get_rect(h)
            return (r1[0] - r0[0], r1[1] - r0[1]), res

        (dx1, dy1), r1 = drag(-160, 90)
        (dx2, dy2), r2 = drag(200, 0)
        assert r1["ok"] and r2["ok"], (r1["detail"], r2["detail"])
        assert abs(dx1 - (-160)) <= 3 and abs(dy1 - 90) <= 3, f"混合拖动 Δ=({dx1},{dy1}) 期望 (-160,90)"
        assert abs(dx2 - 200) <= 3 and abs(dy2) <= 3, f"纯水平拖动 Δ=({dx2},{dy2}) 期望 (200,0)"
        # 最大化窗口：应先还原再拖动，Δ 仍等于光标位移
        W.show_window(h, 3)
        time.sleep(0.7)
        assert W.is_maximized(h), "窗口未最大化"
        r0 = W.get_rect(h)
        x, y = r0[0] + 300, r0[1] + 12
        r3 = b.act(h, "drag", target=f"@{x},{y}", to=f"@{x - 150},{y + 70}")
        time.sleep(0.5)
        r1r = W.get_rect(h)
        assert not W.is_maximized(h), "拖动后窗口仍是最大化（未还原）"
        assert r3["ok"], r3["detail"]
        assert "window_delta" in r3["detail"], r3["detail"]
        return (
            f"混合 Δ=({dx1},{dy1}) 纯水平 Δ=({dx2},{dy2}) 最大化→还原后 ok={r3['ok']} "
            f"({r3['detail'].split('window_delta=')[-1]})"
        )
    finally:
        W.close_window(h)
        time.sleep(0.8)
        if W.is_window(h):
            subprocess.run(["taskkill", "/PID", str(win["pid"]), "/F"], capture_output=True)


def t_modifier_keys_unit() -> str:
    """Bug2 单测：命名键/字母的修饰键解析 + 必须拆成独立 SendInput 事件。"""
    cases = [
        ("shift+f4", {"shift"}, "f4"),
        ("+{F4}", {"shift"}, "F4"),
        ("ctrl+shift+s", {"ctrl", "shift"}, "s"),
        ("alt+f4", {"alt"}, "f4"),
        ("{PGDN}", set(), "PGDN"),
        ("^s", {"ctrl"}, "s"),
        ("{ENTER}", set(), "ENTER"),
        ("ctrl+shift+left", {"ctrl", "shift"}, "left"),
    ]
    for spec, mods, key in cases:
        chords = B._parse_keys(spec)
        assert len(chords) == 1, f"{spec!r} -> {chords}"
        assert chords[0][0] == mods, f"{spec!r} 修饰键 {chords[0][0]} != {mods}"
        assert chords[0][1] == key, f"{spec!r} 主键 {chords[0][1]!r} != {key!r}"
    batches: list[list[tuple[int, int]]] = []
    orig = B._send_inputs
    B._send_inputs = lambda inputs: batches.append(
        [(int(i.u.ki.wVk), int(i.u.ki.dwFlags)) for i in inputs]
    )
    try:
        desc = B.send_keys("shift+f4")
    finally:
        B._send_inputs = orig
    assert desc == "shift+f4", desc
    assert len(batches) == 4, f"应拆成 4 次 SendInput（shift↓/F4↓/F4↑/shift↑），实际 {batches}"
    assert batches[0] == [(0x10, 0)], batches
    assert batches[1] == [(0x73, 0)], batches
    assert batches[2] == [(0x73, 2)], batches
    assert batches[3] == [(0x10, 2)], batches
    return f"{len(cases)} 组解析正确；shift+f4 → 4 次独立事件（shift↓/F4↓/F4↑/shift↑）"


def t_activate_minimized() -> str:
    """Bug1 真机：最小化 / 隐藏(托盘) 状态下 activate 必须还原并拿到前台。"""
    win = _winforms_target("SUPERCU_TEST_ACT")
    h = win["hwnd"]
    try:
        time.sleep(0.7)
        assert W.activate(h, 200), "初始 activate 失败"
        rm = act_retry(h, "minimize")
        assert rm["ok"], rm["detail"]
        time.sleep(0.5)
        assert W.is_minimized(h), "minimize 未生效"
        r1 = act_retry(h, "activate")
        time.sleep(0.35)
        assert r1["ok"], f"最小化窗口 activate 失败: {r1['detail']}"
        assert not W.is_minimized(h), "activate 后仍是最小化"
        assert W.foreground_hwnd() == h, "activate 后没有拿到前台"
        W.show_window(h, 0)  # SW_HIDE，模拟微信在托盘
        time.sleep(0.5)
        assert not W.is_visible(h), "SW_HIDE 未生效"
        r2 = act_retry(h, "activate")
        time.sleep(0.35)
        assert r2["ok"], f"隐藏窗口 activate 失败: {r2['detail']}"
        assert W.is_visible(h), "activate 后窗口仍然隐藏"
        assert W.foreground_hwnd() == h, "隐藏窗口唤回后没有前台"
        return (
            f"最小化→activate ok {r1['ms']}ms detail={r1['detail']!r}；"
            f"隐藏→activate ok {r2['ms']}ms detail={r2['detail']!r}"
        )
    finally:
        W.close_window(h)
        time.sleep(0.8)
        if W.is_window(h):
            subprocess.run(["taskkill", "/PID", str(win["pid"]), "/F"], capture_output=True)


def t_modifier_keys_live() -> str:
    """Bug2 真机：自带 WinForms KeyDown 记录器，验证修饰键真的送达。"""
    log = Path(tempfile.gettempdir()) / "supercu_keylog.txt"
    if log.exists():
        log.unlink()
    lp = str(log).replace("\\", "\\\\")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.Form; $f.Text = 'SUPERCU_KEYLOG'; "
        "$f.Width = 700; $f.Height = 400; $f.StartPosition = 'Manual'; "
        "$f.Left = 500; $f.Top = 380; $f.KeyPreview = $true; "
        "$tb = New-Object System.Windows.Forms.TextBox; $tb.Left = 20; $tb.Top = 20; "
        "$tb.Width = 600; $tb.Height = 200; $tb.Multiline = $true; $f.Controls.Add($tb); "
        f"$logPath = '{lp}'; "
        "$f.add_KeyDown({ param($s,$e) "
        'Add-Content -Path $logPath -Value "$($e.KeyCode)|$($e.Modifiers)" }); '
        "[System.Windows.Forms.Application]::Run($f)"
    )
    before = {w["hwnd"] for w in W.list_windows()}
    proc = subprocess.Popen(["powershell", "-NoProfile", "-STA", "-Command", ps])
    h = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for w in W.list_windows():
            if w["hwnd"] not in before and "SUPERCU_KEYLOG" in (w["title"] or ""):
                h = w["hwnd"]
                break
        if h:
            break
        time.sleep(0.15)
    assert h, "keylog 测试窗口未出现"
    try:
        time.sleep(0.8)

        def read_lines() -> list[str]:
            """PowerShell 的 Add-Content 可能短暂占用文件 → 重试读取。"""
            for _ in range(5):
                try:
                    if log.exists():
                        return [
                            ln.strip()
                            for ln in log.read_text(encoding="utf-8").splitlines()
                            if ln.strip()
                        ]
                    return []
                except OSError:
                    time.sleep(0.15)
            return []

        def send_and_expect(keys: str, want_shift: bool, tag: str) -> None:
            """桌面可能被抢焦点：重激活 + 最多 3 次尝试。"""
            for _ in range(3):
                W.activate(h, 200)
                time.sleep(0.15)
                r = act_retry(h, "key", keys=keys)
                assert r["ok"], (keys, r["detail"])
                time.sleep(0.35)
                seen = [ln for ln in read_lines() if ln.startswith("F4|")]
                if want_shift:
                    if any("Shift" in ln for ln in seen):
                        return
                elif any(ln == "F4|None" for ln in seen):
                    return
            raise AssertionError(f"{tag}: keylog 未记录到预期事件: {seen}")

        send_and_expect("shift+f4", True, "shift+f4")
        send_and_expect("+{F4}", True, "+{F4}")
        send_and_expect("f4", False, "裸 f4")
        lines = read_lines()
        shifted = [ln for ln in lines if ln.startswith("F4|") and "Shift" in ln]
        bare = [ln for ln in lines if ln == "F4|None"]
        return (
            f"shift+f4 / +{{F4}} 均送达 Shift（记录 {len(shifted)} 次 F4|Shift），"
            f"裸 f4 无修饰（{len(bare)} 次）"
        )
    finally:
        W.close_window(h)
        time.sleep(0.8)
        if W.is_window(h):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"], capture_output=True)


def t_hold_unit() -> str:
    """hold：keydown → 持续 ms → keyup（纯单测，不碰桌面）。"""
    events: list[tuple[int, int, float]] = []
    orig = B._send_inputs

    def spy(inputs):
        now = time.perf_counter()
        for i in inputs:
            events.append((int(i.u.ki.wVk), int(i.u.ki.dwFlags), now))

    B._send_inputs = spy
    try:
        t0 = time.perf_counter()
        desc = B.hold_keys("w", 300)
        elapsed = time.perf_counter() - t0
        desc2 = B.hold_keys("shift+w", 120)
    finally:
        B._send_inputs = orig
    assert desc == "w", desc
    assert desc2 == "shift+w", desc2
    assert events[0][0] == 0x57 and events[0][1] == 0, f"首个事件应为 W down: {events[:2]}"
    ups = [e for e in events if e[0] == 0x57 and e[1] == 2]
    assert ups, f"没有 W up: {events}"
    gap = ups[0][2] - events[0][2]
    assert gap >= 0.29, f"按住时长不足: {gap*1000:.0f}ms"
    assert elapsed >= 0.29
    shift_down = [i for i, e in enumerate(events) if e[0] == 0x10 and e[1] == 0]
    shift_up = [i for i, e in enumerate(events) if e[0] == 0x10 and e[1] == 2]
    w_down = [i for i, e in enumerate(events) if e[0] == 0x57 and e[1] == 0]
    w_up = [i for i, e in enumerate(events) if e[0] == 0x57 and e[1] == 2]
    assert shift_down and shift_up and w_down and w_up
    assert shift_down[-1] < w_down[-1] < w_up[-1] < shift_up[-1], f"shift 没有包住 w: {events}"
    return f"w hold 300ms 实测间隔 {gap*1000:.0f}ms；shift+w 顺序 shift↓/w↓/w↑/shift↑ 正确"


def t_hold_mouse_unit() -> str:
    """hold_mouse：down → 心跳移动 → up（纯单测）。"""
    calls: list[tuple] = []
    orig_mouse, orig_move = B._mouse, B._move_cursor_abs
    B._mouse = lambda btn, down: calls.append(("mouse", btn, down))
    B._move_cursor_abs = lambda x, y: calls.append(("move", x, y))
    try:
        t0 = time.perf_counter()
        info = B.hold_mouse("left", 300)
        elapsed = time.perf_counter() - t0
    finally:
        B._mouse, B._move_cursor_abs = orig_mouse, orig_move
    assert calls[0][0] == "mouse" and calls[0][1:] == ("left", True), f"首事件应为 down: {calls[:3]}"
    assert calls[-1][0] == "mouse" and calls[-1][1:] == ("left", False), f"末事件应为 up: {calls[-3:]}"
    beats = [c for c in calls if c[0] == "move"]
    assert len(beats) >= 2, f"心跳移动太少: {beats}"
    assert info["beats"] == len(beats), (info, len(beats))
    assert elapsed >= 0.29, f"按住时长不足: {elapsed*1000:.0f}ms"
    return f"left hold 300ms：down→{len(beats)} 次同位置心跳→up，实测 {elapsed*1000:.0f}ms"


def t_hold_act_plumbing() -> str:
    """act() 插件：ms 截断与 detail 文案（纯单测，不碰桌面）。"""
    got: dict = {}
    orig = (b._ensure_focus, B.hold_keys, B.hold_mouse)
    b._ensure_focus = lambda hwnd: None
    B.hold_keys = lambda keys, ms: (got.__setitem__("key_ms", ms), keys)[1]
    B.hold_mouse = lambda button, ms: (got.__setitem__("mouse_ms", ms), {"beats": 3})[1]
    try:
        r1 = b._run_action(0, "hold", None, {"via": "uia"}, None, "w", None, 0, None, ms=1500)
        r2 = b._run_action(0, "hold_mouse", None, {"via": "uia"}, None, None, None, 0, None, ms=600)
        r3 = b._run_action(0, "hold", None, {"via": "uia"}, None, "w", None, 0, None, ms=99999)
        r4 = b._run_action(0, "hold", None, {"via": "uia"}, None, "w", None, 0, None, ms=5)
        r5 = b._run_action(
            0, "hold_mouse", None, {"via": "uia"}, None, None, None, 0, None, ms=9999, button="right"
        )
    finally:
        b._ensure_focus, B.hold_keys, B.hold_mouse = orig
    assert r1[1] == "hold: w 1500ms", r1
    assert r2[1].startswith("hold_mouse: left 600ms beats=3"), r2
    assert r1[2] and r2[2] and r3[2] and r4[2] and r5[2]
    assert got["mouse_ms"] == 9999 and got["key_ms"] == 20
    assert "截断到 10000" in r3[1], r3
    assert "截断到 20" in r4[1], r4
    for bad in ("abc", [], {}):
        try:
            B._clamp_hold_ms(bad)
            raise AssertionError(f"非法 ms {bad!r} 未报错")
        except B.BridgeError:
            pass
    try:
        B.hold_mouse("wheel", 50)
        raise AssertionError("非法 button 未报错")
    except B.BridgeError as exc:
        assert "left/right/middle" in str(exc)
    assert B._clamp_hold_ms(None) == (500, 500, False)
    return (
        "hold w 1500ms ✅ / hold_mouse left 600ms beats=3 ✅ / 99999→10000 截断 / "
        "5→20 截断 / None→500 默认 / 非法 ms·button 均报错"
    )


def cleanup() -> None:
    h = _calc_hwnd()
    if h and hwnd_alive(h):
        try:
            W.close_window(h)
        except Exception:
            pass
        time.sleep(0.4)
    if h and hwnd_alive(h):
        kill_window_pid(h)
    _CALC.clear()
    hwnd = _NOTEPAD.get("hwnd")
    if hwnd and hwnd_alive(hwnd):
        try:
            W.close_window(hwnd)
        except Exception:
            pass
        time.sleep(0.3)
    if hwnd and hwnd_alive(hwnd):
        kill_window_pid(hwnd)
    # new Notepad keeps one process with many tabs, possibly shared with other
    # agents: only close windows that still point at OUR temp file.
    for w in W.list_windows():
        if w["proc"].lower() == "notepad" and SAVE_PATH.name in (w["title"] or ""):
            try:
                W.close_window(w["hwnd"])
            except Exception:
                pass
    _NOTEPAD.clear()
    if SAVE_PATH.exists():
        try:
            SAVE_PATH.unlink()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 78)
    print("super-cu bridge self-test (T1)")
    print(f"python {sys.version.split()[0]}  {sys.executable}")
    print(f"DPI state: {dpi.ensure_dpi_aware()}")
    print("=" * 78)

    t0 = time.perf_counter()
    check("0.1 dpi/space", t_dpi)
    check("0.2 space three-way", t_space)
    check("1. list_windows", t_list_windows)

    # 12. 非 GUI 单测先跑（不抢桌面）
    check("12.1 key aliases", t_key_aliases)
    check("12.7 modifier+named keys", t_modifier_keys_unit)
    check("12.8 hold (keyboard)", t_hold_unit)
    check("12.9 hold_mouse", t_hold_mouse_unit)
    check("12.10 hold ms clamp/plumbing", t_hold_act_plumbing)
    check("12.6 drag geometry", t_drag_geometry)
    if "--unit-only" in sys.argv[1:]:
        # 纯单测模式：不碰桌面、不写剪贴板（Lead 正在用机器时用这个）
        passed = sum(1 for r in RESULTS if r[1])
        failed = len(RESULTS) - passed
        print("-" * 78)
        print(f"RESULT(unit-only): {passed}/{len(RESULTS)} PASS, {failed} FAIL")
        return 0 if failed == 0 else 1
    check("12.2 clipboard roundtrip", t_clipboard_roundtrip)
    check("12.3 paste fallback", t_paste_fallback)
    check("12.4 disabled / pattern", t_disabled_and_pattern)
    check("12.5 setvalue verify", t_setvalue_verify)
    check("12.6 drag geometry", t_drag_geometry)
    check("12.7 modifier+named keys", t_modifier_keys_unit)

    # 11. UWP 计算器：launch 严格定位 + 树重建语义重定位（P0-1 / P0-2）
    if check("11.0 uwp launch", t_uwp_launch):
        check("11.1 uwp pattern click", t_uwp_click)
        check("11.2 uwp semantic relocate", t_uwp_relocate)
        check("11.3 uwp close", t_uwp_close)

    try:
        _NOTEPAD.update(launch_notepad())
    except Exception as exc:
        print(f"FAIL  setup: 无法启动 Notepad -> {type(exc).__name__}: {exc}")
        RESULTS.append(("setup", False, 0.0, str(exc)))
        _notepad_ok = False
    else:
        _notepad_ok = True
        print(f"      notepad hwnd=#{_NOTEPAD['hwnd']} pid={_NOTEPAD['pid']}")
        check("2.1 snapshot controls", t_snapshot)
        check("2.2 snapshot text view", t_snapshot_text_view)
        check("3. act setvalue", t_setvalue)
        check("3.1 act paste", t_paste_document)
        check("3.2 act drag", t_drag_window)
        check("3.3 activate minimized/hidden", t_activate_minimized)
        check("3.4 modifier keys live", t_modifier_keys_live)
        check("4. key/type/invoke save", t_save_chain)
        check("5. gui_locked coverage", t_lock_wrapped)
        check("6. ambiguity refusal", t_class_ambiguous)
        check("7. screenshot", t_screenshot)
        check("8. wait", t_wait)
        check("9. edge snapshot(read)", t_edge_snapshot)
        check("9.1 heavy window perf", t_heavy_perf)
        check("10. act close", t_close)

    cleanup()
    total_ms = (time.perf_counter() - t0) * 1000
    passed = sum(1 for r in RESULTS if r[1])
    failed = len(RESULTS) - passed
    print("-" * 78)
    print(f"RESULT: {passed}/{len(RESULTS)} PASS, {failed} FAIL   total {total_ms:.0f}ms")
    if failed:
        for name, ok, ms, detail in RESULTS:
            if not ok:
                print(f"  FAILED {name}: {detail}")
    print("metrics:")
    for k, v in STATS.items():
        if k.startswith("reuse"):
            print(f"  {k} = {v:.1%}")
        elif isinstance(v, float) and v.is_integer() and v > 1000:
            print(f"  {k} = {int(v)}")
        else:
            print(f"  {k} = {v}")
    if ACTIONS:
        ok_actions = sum(1 for _n, ok in ACTIONS if ok)
        print(f"  action_success = {ok_actions}/{len(ACTIONS)} = {ok_actions / len(ACTIONS):.0%}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

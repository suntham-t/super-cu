# Changelog

## 1.5.0 — 2026-10-27

### Added

- scroll_until_visible：滚轮版滚到元素可见，覆盖微信/Electron 这类没有 ScrollPattern 的自绘列表；
  按 rect 判定可见性，报告滚动步数，无进展时给建议后失败。
- drag.to_window + drag.to:"#id"：跨窗口拖放，在目标窗口里解析元素中心作为落点；drag.auto_scroll
  先把源元素滚进视口。全部可选，旧调用不变。

### Fixed

- drag 不再静默成功：起点在窗口外、落点不在目标窗口、drop 被其他窗口接走都会直接报错；
  成功时 detail 带 drop_window=#hwnd pid=.. class=..。
- cu_show 的 offscreen 改为 rect 与窗口交集判定（不再依赖 UIA IsOffscreen，微信消息列表里为 false）。

### Changed

- 内联状态不再固定等 150ms：先立即快照，只有看起来没变且步骤是换视图类（click/invoke/...）或失败时
  才等 80-200ms 重试一次；快路径不再付固定税。

### Tests

- mock 协议套件 223/223、bridge 单元 27/27（开发自测）；真实套件结果本版不在此报告。

## 1.4.0 — 2026-10-27

### Added

- **OpenAI CUA driver action-surface parity.** The nine computer-use driver actions documented by OpenAI
  (screenshot / click / double_click / drag / keypress / move / scroll / type / wait, including button,
  held keys, scroll_x/scroll_y and drag path) are now all expressible through `cu_do`:
  - `click` gains `button`: left / right / middle (wheel) / back / forward (XBUTTON1/2);
  - `scroll` gains `axis`: `x` (horizontal wheel / horizontal ScrollPattern) or `y`;
  - `drag` gains `path`: an arbitrary list of coordinate waypoints (CUA `drag.path`), `to` becomes
    optional when a path is given;
  - pointer actions gain `modifiers` (ctrl/shift/alt/win) held while clicking/dragging/scrolling, the
    equivalent of CUA `keys`.
  All new fields are optional; existing calls and defaults are unchanged. Semantic actions, stable ids,
  batched fail-fast, `expect` and minimized-window background mode are untouched.
- `docs/CUA-PARITY.md` — action-by-action mapping, 2026 OpenAI architecture summary, benchmark caveats,
  what we deliberately do not copy, and real-GUI verification results. `docs/RESEARCH.md` gains
  §6 (OpenAI CUA) and updated references; SKILL/README document the new fields.

### Tests

- real bridge suite **57/57 PASS**, mock protocol suite **216/216 PASS**, bridge unit suite **26/26 PASS**,
  py_compile clean.
- New unit test 14.6: XBUTTON mapping, modifier normalization and press/release ordering, horizontal wheel
  routing, drag-path point parsing. New mock suite section: CUA schema, valid combinations, invalid values
  (-32602).
- Real GUI: mouse back button navigated an Explorer window from `subdir` back to its parent; 4-waypoint
  drag path on Notepad; middle click; ctrl+shift click; horizontal wheel.

## 1.3.1 — 2026-10-27

### Fixed

- **cu_do inline state raced the repaint after view-switching clicks.** With `observe=auto/state` the
  inline snapshot was taken immediately after the action; apps such as WeChat repaint the new pane a
  few hundred ms later, so the state block still showed the OLD view (and an `expect` on a text that
  already existed in the old view — e.g. the search box — could confirm a switch that had not
  rendered yet). The inline snapshot now waits a bounded grace (150-250 ms, derived from `settle_ms`)
  after a changed or failed step before rendering. Best effort: apps slower than ~250 ms (WeChat chat
  loading can be ~300 ms) may still show the previous pane; a follow-up `cu_show` is the reliable check.
- Mock backend (instantaneous) skips the grace so the protocol suite keeps its speed.

### Notes

- No schema or behavior change for actions themselves; only the timing of the inline observation.
- Docs: `SKILL.md` gains a "view-switch state check" note (do not trust `expect` alone, re-`cu_show`
  after view-switching clicks) and README documents the timing window.

## 1.3.0 — 2026-10-27

### Added

- **Minimized-window background actions.** `cu_do` can now run pure UIA-pattern actions
  (`invoke` / `set` / `toggle` / `select` / `expand` / `collapse`) on a **minimized**
  window **without restoring it**. Pattern steps on a minimized window switch to background mode
  automatically; explicit control via per-step `background: true|false` or batch-level
  `{"background": true, "steps": [...]}`. Input-synthesis actions (`click` / `dblclick` /
  `rightclick` / `hover` / `drag` / `scroll` / `type` / `key` / `paste` / `focus`) are
  rejected in background mode with rewrite advice in 0 ms instead of silently stealing focus.
- `cu_show` window headers now carry `min` / `max`; snapshots expose `minimized` /
  `maximized` (the presentation layer already knew how to render them).
- `bridge.act(background=True)` restricts itself to the pattern whitelist (no click fallback, no
  focus/clipboard fallback for `setvalue`) and reports `focus_stolen` when the target app's own
  UIA provider grabs the foreground — WeChat does this even while minimized.
- `docs/MINIMIZED.md` — capability matrix, usage, per-app findings.

### Changed

- `cu_do` step lines gain `bg` / `focus_stolen` markers; the step schema advertises
  `background`; a batch-level `background` option is new.
- `tests/test_bridge.py`: new background-policy unit test; the UWP relocate test now also clears
  `cached_element` so it really exercises semantic relocation (it could pass through the cache
  fast path by accident before).

### Tests

- real bridge suite 56/56 PASS, mock protocol suite 205/205 PASS, bridge unit suite 25/25 PASS,
  py_compile clean. New unit/route coverage: background policy whitelist, background step marker,
  batch-level background, background + input-synthesis rejection.

### Notes

- Minimized windows report an icon-position rect (`-31991,-32000` style): never derive coordinates
  from it.
- Background pattern actions need neither the foreground nor the shared desktop lock, so they do not
  disturb another agent/session using the desktop.
- Application-dependent: Windows 11 Notepad does not expose its document tree while minimized
  (restore first); WeChat exposes the full tree and accepts value/invoke in background.

## 1.2.0 — 2026-10-27

Performance round: snapshot fast path over UIA cache requests, lazy startup with after-handshake
prefetch, bounded long-session memory. **No tool schema, text format or snapshot-contract change**
(`compact.py` and `windows.py` are byte-identical to 1.1.2).

### Performance (paired interleaved A/B vs 1.1.2, same machine, see `docs/OPT-AFTER.md`)

- **`cu_show` snapshot**: one `BuildUpdatedCache(RawViewCondition, Subtree)` call per window instead of
  ~9 cross-process property reads per node plus RawViewWalker child calls; cached properties are read
  locally. Large Chromium window 218.4 -> 28.3 ms (-86.3%, 8.6x at `max_nodes=400`); Explorer System32
  217.8 -> 129.0 ms (-39.5%); Notepad 169.2 -> 91.8 ms (-44.5%). Same visited/shown counts.
- **Cold start**: `initialize`/`tools/list` no longer import uiautomation/Pillow. spawn->initialize
  859.7 -> 255.8 ms p50 (-70.8%; -95.4% after subtracting the interpreter floor). A raw thread prefetches
  the modules after the handshake without blocking the protocol loop: `tools/list`->first `cu_show`
  480.2 -> 12.2 ms when the client pauses at all.
- **Warm end-to-end** `cu_show`: 144.6 -> 94.6 ms (-40.6%). Render output unchanged (30,313 chars /
  11,659 tokens_est on the 11 fixture-view pairs, byte-identical).
- **Memory**: element-id map capped at 20,000 (LRU, ids never reused) and window caches at 32.
  Synthetic 80,000-id churn: +45.0 MB -> +12.4 MB RSS, newest ids still resolve.
- **cu_do**: `settle_ms` stays an upper bound; only set/focus steps that changed the UI through a
  synchronous UIA pattern (and are not last / carry no expect) may end the wait early after two cheap
  win32 probes. Real 5-step Notepad batch: 1230.2 -> 1035.6 ms p50 (-15.8%). Input-synthesis batches
  unchanged.

### Fixed

- **COM apartment race with the import prefetch**: comtypes calls `CoInitializeEx` in whichever thread
  imports it first; when the prefetch finished before the first tool call, the first real `cu_show`
  failed with `OSError: [WinError -2147221008] 尚未调用 CoInitialize`. `Bridge.__init__` now claims a COM
  apartment on the requesting thread before any UIA object; covered by a unit test and a spawned-process
  regression test.
- `by_id()` miss message now mentions id recycling after very long sessions.

### Added

- `SUPER_CU_NO_CACHE=1` (force the 1.1.2 per-node walk), `SUPER_CU_NO_PREFETCH=1` (disable module
  prefetch), `SUPER_CU_SETTLE_MIN_MS` (force blind settle floor). All are escape hatches; defaults
  enable the new fast paths.
- `bench/opt/bench.py` (reproducible harness: cold start / snapshot / render / screenshot / e2e /
  memory + interleaved paired A/B + frozen fixtures) and `bench/verify/` (independent parity,
  golden, protocol and performance gates).
- `docs/OPTIMIZATION.md` (this round), `docs/OPT-BASELINE.md` (1.1.2 baseline),
  `docs/OPT-AFTER.md` (paired A/B), `docs/OPT-VERIFY.md` (independent sign-off).

### Safety

- Every cache/COM error logs once to stderr and falls back to the legacy walk; a window whose cache
  build exceeds the 250 ms budget or keeps failing is served by the legacy path for 60 s; output is
  always identical.
- Tests: real bridge suite 55/55 PASS, mock protocol suite 197/197 PASS, bridge unit suite 24/24,
  py_compile clean.

## 1.1.2 — 2026-10-24

### Changed
- **cu_do no longer passes a duplicate t= to the drag action.** The server's drag step passed an extra
  t= argument, which routed ridge.act through its point-only branch instead of the
  element/target path used by the verified direct call. The duplicate is gone and a
  passthrough test asserts the exact call shape (target/to/hold_ms, no t).
  **The caption-drag horizontal issue is NOT fixed by this** — bridge-level drags move
  both axes, but the same drag driven through cu_do still moves only vertically.
  Reproduction, DEBUG output and the five-round investigation live in
  ench/regress-report.md; tracked as a known issue in the README.
- SUPER_CU_DEBUG_DRAG=1 prints one stderr line per drag (target/at/to/hold_ms) for field
  diagnosis; never part of the protocol output.

## 1.1.1 — 2026-10-24

### Fixed
- **Bridge-level caption drags follow the cursor again (server path still under investigation).** An earlier optimisation routed every drag step
  through SendInput(MOUSEEVENTF_ABSOLUTE|VIRTUALDESK) so raw-input apps (Blender, games)
  could see the motion; Windows' modal window-move loop only follows *real* cursor movement,
  so the window stopped following horizontally. Caption drags now step with SetCursorPos
  only; content drags keep the injected heartbeat that raw-input apps need. New unit test
  asserts the routing (caption => SetCursorPos only) so the bug cannot come back.
  **Known issue:** driving the same drag through the cu_do tool still loses the
  horizontal component (bridge-level call verified OK); reproduction is in
  ench/regress-report.md.
- Drag verification is now expected-delta aware: the expectation is clamped to the virtual
  screen before comparing (exp_wdelta= appears in the detail line), so legitimate
  screen-edge clamping is no longer reported as a failed drag.

## 1.1.0 — 2026-10-24

Capability round: five opt-in additions, no change to any existing default path.
Prioritised after reading comparable projects (`docs/RESEARCH.md`): Windows-MCP,
microsoft/OmniParser, Anthropic computer-use, browser-use, trycua/cua, UFO.

### Added
- **`cu_show {\\"marks\\": true}`** — Set-of-Mark screenshots: every actionable element is
  drawn with a numbered box matching its `#id`, so a vision model can point at what it sees
  and the agent can still act by id. Off by default; without the flag the response body is
  byte-for-byte what it was.
- **`clipboard` step** (`mode: get|set`) — read or write the Windows clipboard through the
  leak-safe implementation (write, verify by reading back, restore the user's clipboard).
- **`window_move` step** (`x, y[, w, h]`) — move/resize a window so it is not covered by
  another one; clamped to the virtual screen, maximized windows are restored first.
- **`scroll_to` step** — bring an element into view (`ScrollItemPattern.ScrollIntoView`,
  else a bounded parent `ScrollPattern` scroll, else a loud failure).
- **`drag.to` accepts element targets** — `to: "#123"` or `to: "button:发送"` drags onto the
  centre of that element (drag files into a chat, drop items onto a target).
- **`cu_show {scope: "desktop"}`** marks windows that belong to the same process
  (`[同进程 xN]`), which is what Office's SDI multi-window model needs.
- **`docs/RESEARCH.md`** — the comparison that drove this round, including what was
  deliberately rejected (vision-model parsing, an OS-tool grab bag, a built-in planner).

### Fixed
- `drag` no longer reports failure for content drags: the "did the window follow the cursor"
  check now applies only when the drag starts on the title bar (where it was introduced to
  catch the maximized-window Δx=0 bug). Title-bar drags keep the strict verification.

### Docs
- `SKILL.md`: Office SDI window selection (`cu_show scope=desktop` → `#hwnd`), Excel
  `Ctrl+G` "Go To" for exact ranges (do not `set` the Name Box) and F12 for Save As,
  plus the new ops.
- CI workflow (`.github/workflows/checks.yml`): byte-compile, protocol tests, bridge unit
  tests, and manifest checks (dsh.bundle.patch present, patch YAML inserts the MCP row).

## 1.0.0 — 2026-10-24

First public release: `cu_show` / `cu_do` over UI Automation, stable element ids,
pattern-first actions, DPI-space unification, normalized `@n` fallback coordinates,
batched steps with `expect`/`wait`, GUI lock, and the v1 verification suite
(40 bridge tests, 160 protocol tests).

# Changelog

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

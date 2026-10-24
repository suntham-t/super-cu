# Changelog

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

# super-cu AFTER benchmark - paired interleaved A/B (1.2.0 candidate vs pristine)

> **Status**: pristine baseline captured 2026-10-27T09:55:04+0800 (bench/opt/baseline.json). Paired interleaved A/B below: A=D:\opencode\_supercu_opt\orig\src vs B=D:\opencode\plugins\super-cu\src, label after, 5 rounds, generated 2026-10-27T10:16:06+0800. Candidate first real cu_show ok: yes; source trees frozen during the run: yes. Screenshot section: candidate single-src run after-screenshot. Never compare absolute numbers across different runs on this shared machine.

- label: **baseline**
- generated_at: 2026-10-27T09:55:04+0800
- schema: 1 (harness 1.0.0)
- quick mode: no
- duration: 68.4 s
- host: Windows-11-10.0.26200-SP0 / python 3.13.15

## Source under test

- src_dir: D:\opencode\_supercu_opt\orig\src

| module | sha256 (first 16) | bytes |
|---|---|---|
| bridge.py | 5974fc395c921f59 | 110597 |
| compact.py | 8d0e328c4f5514fa | 30153 |
| dpi.py | 64d1e22390b7add6 | 3517 |
| gui_lock.py | 479e6a7004c9c398 | 2315 |
| mock_bridge.py | 4ec91bbde460acbb | 31277 |
| windows.py | c07bb57a00fbc6a8 | 27397 |

## Machine state (covariates)

| signal | start | end |
|---|---|---|
| captured_at | 2026-10-27T09:55:05+0800 | 2026-10-27T09:56:13+0800 |
| cpu_load_pct | 49.5 | 21.8 |
| python_processes | 10 | 13 |
| visible_windows | 26 | 26 |
| own_rss_mb | 47.81 | 48.15 |
| foreground | DeepSeek Harness: 计算概论终端前端优化 — DeepSeek Harness | DeepSeek Harness: 计算概论终端前端优化 — DeepSeek Harness |

## Cold start (spawn -> initialize -> tools/list -> cu_show)

| mode | n | raw init p50 ms | raw init p95 ms | noop python p50 ms | adjusted init p50 ms | adjusted init p95 ms | spawn->tools/list p50 ms | spawn->first cu_show p50 ms | child RSS p50 MB | first show ok |
|---|---|---|---|---|---|---|---|---|---|---|
| real | 5 | 923.1 | 1047.2 | 233.9 | 717.7 | 765.9 | 923.3 | 932.8 | 39.5 | yes |
| mock | 5 | 418.1 | 459.2 | 206.6 | 207.4 | 265.8 | 418.3 | 418.4 | 23.2 | yes |

## Snapshot (in-process bridge.snapshot, controls view unless noted)

| target | view | n | visited p50 | shown p50 | wall p50 ms | wall p95 ms | ms/visited p50 | ms/visited p95 | walk p50 ms | make_node p50 ms | control_for p50 ms | space_check p50 ms | outlier retries |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| notepad_controlled | controls | 10 | 82 | 61 | 169.2 | 210.0 | 2.064 | 2.561 | 123.6 | 44.3 | 0.0 | 0.0 | 0 |
| explorer_system32 | controls | 10 | 128 | 90 | 235.0 | 295.9 | 1.836 | 2.312 | 176.4 | 54.7 | 0.0 | 0.0 | 0 |
| large_real_window | controls | 10 | 376 | 216 | 366.6 | 422.7 | 0.975 | 1.124 | 221.8 | 145.6 | 0.0 | 0.0 | 0 |
| notepad_controlled | all | 1 | 82 | 79 | 157.8 | 157.8 | 1.925 | 1.925 | 103.6 | 53.6 | 0.0 | 0.0 | 0 |

## Render (compact.format_snapshot on frozen fixtures)

| fixture | kind | view | chars | bytes | lines | tokens_est | p50 ms | p95 ms | tree_cut | budget_cropped | hidden | stable |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| calculator | real | controls | 1078 | 1330 | 42 | 414.6 | 0.20 | 0.26 | no | no | 0 | yes |
| calculator | real | all | 1424 | 1848 | 56 | 547.7 | 0.18 | 0.27 | no | no | 0 | yes |
| explorer_system32 | real | controls | 1956 | 2366 | 68 | 752.3 | 0.37 | 0.43 | no | no | 0 | yes |
| explorer_system32 | real | all | 3192 | 4080 | 108 | 1227.7 | 0.36 | 0.58 | no | no | 0 | yes |
| mock_edge | mock | controls | 1297 | 1634 | 43 | 498.8 | 0.15 | 0.16 | no | no | 0 | yes |
| mock_edge | mock | all | 1238 | 1565 | 41 | 476.2 | 0.11 | 0.15 | no | no | 0 | yes |
| mock_large | mock | controls | 3274 | 4282 | 103 | 1259.2 | 1.93 | 2.13 | yes | yes | 300 | yes |
| mock_large | mock | all | 10781 | 14625 | 387 | 4146.5 | 1.44 | 1.66 | yes | yes | 16 | yes |
| notepad | real | controls | 1779 | 2281 | 58 | 684.2 | 0.20 | 0.26 | no | no | 0 | yes |
| notepad | real | all | 2339 | 2887 | 79 | 899.6 | 0.20 | 0.40 | no | no | 0 | yes |
| notepad | real | text | 1955 | 2475 | 61 | 751.9 | 0.15 | 0.17 | no | no | 0 | yes |

- totals: 11 fixture/view pairs, 30313 chars, 11658.7 tokens_est

## Screenshot

| shot | n | wall p50 ms | wall p95 ms | JPEG bytes p50 | image |
|---|---|---|---|---|---|
| window | 5 | 43.3 | 112.0 | 181469 | 1166x873 |
| region [87, 130, 480, 360] | 1 | 43.1 | - | 51958 | - |

## End-to-end spawned server (cu_show on controlled Notepad)

| n | wall p50 ms | wall p95 ms | chars p50 | spawn->init ms | spawn->first show ms | ok |
|---|---|---|---|---|---|---|
| 5 | 130.3 | 147.6 | 1882 | 889 | 1077 | yes |

## Memory growth (200 sequential snapshots)

| map / metric | start | end | delta |
|---|---|---|---|
| _rid2id | 61 | 61 | 0 |
| _id2el | 61 | 61 | 0 |
| _cache | 1 | 1 | 0 |
| _win_ctl | 1 | 1 | 0 |
| _win_info | 1 | 1 | 0 |
| RSS MB | 59.96 | 44.51 | -15.45 |
| snapshot p50 ms | - | - | 141.73 |

### Fixtures (frozen, generated from pristine src)

- fixtures_dir: D:\opencode\plugins\super-cu\bench\opt\fixtures

| file | sha256 (first 16) | bytes |
|---|---|---|
| calculator.json | c62e853f297c5139 | 39554 |
| explorer_system32.json | 40d42adcf23a9ffa | 94466 |
| manifest.json | 05ff99f8f980350b | 4284 |
| mock_edge.json | 08702f878e1a38bd | 34107 |
| mock_large.json | bb519c0cdaff552b | 290250 |
| notepad.json | 870b16f25b3caf15 | 84008 |

## Controlled window (Notepad)

- window: Notepad #420158788 rect=[67, 60, 1233, 933]
- fresh launch verified: not recorded (captured before the field existed); matched=True, popup check: modal_children=False, other_notepad_windows=0, dismiss_actions=0
- setvalue: ok=True via=setvalue ms=531 verified=True
- content_verified: True (41 lines, sha256=1cc676a54beb6e74)
- rect after window_move: [67, 60, 1233, 933]
- re-launches during run: 0
- cleanup: closed=True forced=False


---

# Paired interleaved A/B (AFTER: candidate vs pristine)

- label: **after**
- generated_at: 2026-10-27T10:16:06+0800
- schema: 1 (harness 1.0.0)
- quick mode: no
- rounds: 5 (A and B interleaved, order flips every round)

## Sources

- A (baseline): D:\opencode\_supercu_opt\orig\src
- B (candidate): D:\opencode\plugins\super-cu\src

| module | A sha256 (16) | B sha256 (16) | relation |
|---|---|---|---|
| bridge.py | 5974fc395c921f59 | a9b18446633cd249 | DIFF |
| compact.py | 8d0e328c4f5514fa | 8d0e328c4f5514fa | same |
| dpi.py | 64d1e22390b7add6 | 64d1e22390b7add6 | same |
| gui_lock.py | 479e6a7004c9c398 | 479e6a7004c9c398 | same |
| mock_bridge.py | 4ec91bbde460acbb | 4ec91bbde460acbb | same |
| server.py | 44c0bb020f57ad5c | e019e244a81a5e0a | DIFF |
| windows.py | c07bb57a00fbc6a8 | c07bb57a00fbc6a8 | same |

- candidate first real cu_show ok: yes (e2e error calls=0, cold-start first-show errors=0)
- source trees frozen during the run: yes

## Machine state (covariates)

| signal | start | end |
|---|---|---|
| captured_at | 2026-10-27T10:16:07+0800 | 2026-10-27T10:17:19+0800 |
| cpu_load_pct | 30.1 | 22.5 |
| python_processes | 10 | 9 |
| visible_windows | 26 | 26 |
| own_rss_mb | 49.83 | 47.48 |
| foreground | DeepSeek Harness: 优化本地插件 super-cu — DeepSeek Harness | DeepSeek Harness: 优化本地插件 super-cu — DeepSeek Harness |

## Controlled window (Notepad)

- window: Notepad #8791802 rect=[67, 60, 1233, 933]
- fresh launch verified: True, popup check: modal_children=False, other_notepad_windows=0, dismiss_actions=0
- setvalue: ok=True via=setvalue ms=537 verified=True
- content_verified: True (41 lines, sha256=1cc676a54beb6e74)
- rect after window_move: [67, 60, 1233, 933]
- re-launches during run: 0
- cleanup: closed=True forced=False

## Headline (paired medians, B relative to A)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| cold start real adjusted init | 668.4 ms | 33.5 ms | -616.3 ms | -95.4% | 100% |
| cold start real raw init | 859.7 ms | 255.8 ms | -616.3 ms | -70.8% | 100% |
| cold start real spawn->first cu_show | 871.4 ms | 844.1 ms | -89.6 ms | -10.5% | 80% |
| cold start real init->first cu_show | 8.7 ms | 588.3 ms | 578.3 ms | 6172.1% | 0% |
| cold start mock adjusted init | 210.3 ms | 71.2 ms | -119.9 ms | -69.9% | 100% |
| cold start mock raw init | 382.2 ms | 257.9 ms | -119.9 ms | -31.4% | 100% |
| cold start mock spawn->first cu_show | 382.6 ms | 267.3 ms | -114.1 ms | -29.8% | 100% |
| cold start mock init->first cu_show | 0.4 ms | 6.7 ms | 6.3 ms | 1741.2% | 0% |
| snapshot notepad_controlled ms/visited | 1.923 | 1.043 | -0.867 | -44.5% | 100% |
| snapshot explorer_system32 ms/visited | 1.702 | 1.008 | -0.672 | -39.5% | 100% |
| snapshot large_real_window ms/visited | 1.021 | 0.132 | -0.893 | -86.3% | 100% |
| render chars total | 30313 | 30313 | 0 | 0.0% | - |
| e2e cu_show median call | 144.6 ms | 94.6 ms | -58.6 ms | -40.6% | 100% |

## Cold start

| mode | A raw init p50 ms | B raw init p50 ms | raw delta % | A adjusted p50 ms | B adjusted p50 ms | adjusted delta % | A ->tools p50 | B ->tools p50 | A ->first show p50 | B ->first show p50 | first show delta % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| real | 859.7 | 255.8 | -70.8% | 668.4 | 33.5 | -95.4% | 860.0 | 256.1 | 871.4 | 844.1 | -10.5% |
| mock | 382.2 | 257.9 | -31.4% | 210.3 | 71.2 | -69.9% | 382.5 | 258.8 | 382.6 | 267.3 | -29.8% |

- first real cu_show errors (MUST be 0; nonzero means the CoInitialize/COM race or a broken spawn): real A=0 B=0; mock A=0 B=0

## Snapshot (interleaved, controls view)

### notepad_controlled (#8791802 Notepad, read_only=False, outlier retries=0)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| ms/visited | 1.923 | 1.043 | -0.867 | -44.5% | 100% |
| wall ms | 169.23 | 91.78 | -76.30 | -44.5% | 100% |
| bridge elapsed ms | 169.0 | 92.0 | -76.0 | -44.5% | 100% |
| visited nodes | 88 | 88 | 0 | 0.0% | 0% |
| shown nodes | 63 | 63 | 0 | 0.0% | 0% |
| walk ms | 120.78 | 0.00 | -120.78 | -100.0% | 100% |
| make_node ms | 47.79 | 0.00 | -47.79 | -100.0% | 100% |
| control_for ms | 0.00 | 0.00 | -0.00 | -50.0% | 80% |

### explorer_system32 (#2895342 explorer, read_only=False, outlier retries=0)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| ms/visited | 1.702 | 1.008 | -0.672 | -39.5% | 100% |
| wall ms | 217.80 | 128.98 | -86.00 | -39.5% | 100% |
| bridge elapsed ms | 218.0 | 129.0 | -86.0 | -39.4% | 100% |
| visited nodes | 128 | 128 | 0 | 0.0% | 0% |
| shown nodes | 90 | 90 | 0 | 0.0% | 0% |
| walk ms | 166.36 | 0.00 | -166.36 | -100.0% | 100% |
| make_node ms | 48.77 | 0.00 | -48.77 | -100.0% | 100% |
| control_for ms | 0.00 | 0.00 | 0.00 | 0.0% | 0% |

### large_real_window (#54596812 DeepSeek Harness, read_only=True, outlier retries=0)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| ms/visited | 1.021 | 0.132 | -0.893 | -86.3% | 100% |
| wall ms | 218.43 | 28.29 | -191.15 | -86.3% | 100% |
| bridge elapsed ms | 218.0 | 28.0 | -191.0 | -86.5% | 100% |
| visited nodes | 214 | 214 | 0 | 0.0% | 0% |
| shown nodes | 136 | 136 | 0 | 0.0% | 0% |
| walk ms | 126.86 | 0.00 | -126.86 | -100.0% | 100% |
| make_node ms | 93.16 | 0.00 | -93.16 | -100.0% | 100% |
| control_for ms | 0.00 | 0.00 | 0.00 | 0.0% | 20% |

## Render (compact.format_snapshot on frozen fixtures)

| fixture | view | A chars | B chars | chars delta % | A tokens | B tokens | A p50 ms | B p50 ms | ms delta % | identical text |
|---|---|---|---|---|---|---|---|---|---|---|
| calculator | controls | 1078 | 1078 | 0.0% | 414.6 | 414.6 | 0.14 | 0.14 | -0.2% | yes |
| calculator | all | 1424 | 1424 | 0.0% | 547.7 | 547.7 | 0.15 | 0.15 | 0.9% | yes |
| explorer_system32 | controls | 1956 | 1956 | 0.0% | 752.3 | 752.3 | 0.35 | 0.35 | 0.5% | yes |
| explorer_system32 | all | 3192 | 3192 | 0.0% | 1227.7 | 1227.7 | 0.44 | 0.38 | -0.8% | yes |
| mock_edge | controls | 1297 | 1297 | 0.0% | 498.8 | 498.8 | 0.18 | 0.17 | -0.9% | yes |
| mock_edge | all | 1238 | 1238 | 0.0% | 476.2 | 476.2 | 0.11 | 0.11 | -0.1% | yes |
| mock_large | controls | 3274 | 3274 | 0.0% | 1259.2 | 1259.2 | 2.01 | 2.04 | 1.2% | yes |
| mock_large | all | 10781 | 10781 | 0.0% | 4146.5 | 4146.5 | 1.09 | 1.08 | -1.7% | yes |
| notepad | controls | 1779 | 1779 | 0.0% | 684.2 | 684.2 | 0.28 | 0.28 | -1.4% | yes |
| notepad | all | 2339 | 2339 | 0.0% | 899.6 | 899.6 | 0.29 | 0.28 | -0.0% | yes |
| notepad | text | 1955 | 1955 | 0.0% | 751.9 | 751.9 | 0.20 | 0.20 | -0.7% | yes |

- totals: A chars=30313, B chars=30313 (delta 0.0%), identical outputs 11/11

## End-to-end spawned server (cu_show on controlled Notepad)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| median cu_show call ms | 144.6 ms | 94.6 ms | -58.6 ms | -40.6% | 100% |
| median output chars | 1868 | 1867 | -1 | -0.1% | 100% |

- cu_show error calls (MUST be 0): A=0 B=0

## Memory growth (sequential snapshots)

| side | n | rid2id end | id2el end | cache end | RSS start MB | RSS end MB | RSS delta MB | snapshot p50 ms |
|---|---|---|---|---|---|---|---|---|
| A | 100 | 63 | 63 | 1 | 46.98 | 47.1 | 0.12 | 136.38 |
| B | 100 | 63 | 63 | 1 | 47.12 | 47.12 | 0.0 | 86.26 |

### Fixtures (frozen, generated from pristine src)

- fixtures_dir: D:\opencode\plugins\super-cu\bench\opt\fixtures

| file | sha256 (first 16) | bytes |
|---|---|---|
| calculator.json | c62e853f297c5139 | 39554 |
| explorer_system32.json | 40d42adcf23a9ffa | 94466 |
| manifest.json | 05ff99f8f980350b | 4284 |
| mock_edge.json | 08702f878e1a38bd | 34107 |
| mock_large.json | bb519c0cdaff552b | 290250 |
| notepad.json | 870b16f25b3caf15 | 84008 |


---

## Screenshot (candidate single-src run vs pristine baseline)

- screenshot is not part of the interleaved harness (identical code path); this compares two single-src runs: baseline label=baseline vs candidate label=after-screenshot generated 2026-10-27T10:17:20+0800

| metric | baseline (A) | candidate (B) | delta % |
|---|---|---|---|
| window shot wall p50 ms | 43.3 | 43.1 | -0.4% |
| window shot wall p95 ms | 112.0 | 45.9 | - |
| window JPEG bytes p50 | 181469 | 179681 | -1.0% |
| region wall ms [87, 130, 480, 360] | 43.1 | 42.8 | -0.7% |
| region JPEG bytes [87, 130, 480, 360] | 51958 | 52020 | 0.1% |

---

## Method

- INTERLEAVED A/B: both src trees are loaded as isolated module sets in one process; A and B are measured alternately (order flips every round) so the paired median delta cancels machine-load drift. Subprocess metrics alternate spawned servers A and B.

- Harness: bench/opt/bench.py, schema 1. Every number in this report is read from the recorded JSON file(s) named below; nothing is typed by hand.
- Percentiles: linear interpolation over the recorded samples; p50 is the median. p95 from small n is indicative only.
- Snapshot: in-process bridge.snapshot(hwnd, view, max_nodes) on the controlled Notepad window, on our own Explorer/System32 window, and on a large real window (read-only: no focus change, no input; the DeepSeek Harness/Chromium window is preferred so before/after use the same app, falling back to the largest visible window). Bridge methods (snapshot/_walk/_make_node/_control_for/space_check) are wrapped by the harness to report the internal breakdown; src is not modified.
- ms/visited = wall_ms / bridge-reported total (visited UIA nodes) and is the load-robust headline metric; absolute milliseconds drift with CPU load.
- Controlled window: Notepad opened on a fixed 41-line ASCII+Chinese document, content set through bridge setvalue (verified by read-back), window moved to a fixed rect, saved and closed at the end of the run.
- Render: compact.format_snapshot over frozen fixture JSON captured from the pristine src; controls view uses max_lines=100 (server default). tokens_est = chars / 2.6 because no tokenizer is installed on this host (method recorded in JSON).
- Cold start: spawn python -u src/server.py (real, and --mock), then initialize, tools/list and first cu_show scope=desktop. raw includes interpreter startup; adjusted subtracts a paired interpreter-startup spawn measured in the same round over the same transport (spawn until the first stdout line, then kill).
- E2E: spawned real server, cu_show on the controlled Notepad window, wall-clock measured client side.
- Memory: sequential snapshots on the controlled window from a fresh Bridge; id-map sizes and process RSS sampled every n/4 snapshots.
- Screenshot: bridge.screenshot(notepad) plus one region capture.
- Each section records CPU load, python process count, visible window count and node counts as covariates.

## Caveats

- This machine is shared: the user's live desktop session and other agents' benchmarks run concurrently, so raw latency drifts 30-50%. Quote ms/visited and paired deltas, not absolute numbers across runs.
- The large real window is measured read-only and deliberately NOT frozen into a fixture (it contains user data); its title changes with the user's session.
- Notepad on this host uses English UI strings; fixture window titles are therefore English even though the controlled content is ASCII+Chinese.
- Screenshot ms/bytes depend on window size, DPI and occlusion; the region capture is clamped to the physical screen.
- Cold-start adjusted values subtract a paired interpreter startup (spawn until first stdout line, measured in the same round). The interpreter itself is slower under load (observed ~0.3s idle vs ~1.4s loaded), so adjusted is a better comparison unit than raw. A negative adjusted value means the server reached initialize at or below the interpreter-startup floor: there is no measurable import cost left, and the residual is spawn-scheduler noise (~100-300ms), not a real negative latency.
- Baseline absolute numbers were captured on a shared machine while other agents were benchmarking (see Machine state: CPU load and python process counts at start/end). They describe that run, not a constant; the authoritative before/after evidence is the paired interleaved mode.
- The memory section measures a STABLE controlled window, so id-maps can show zero growth and process RSS can even shrink (GC/allocator). The unbounded-growth risk appears when the element set changes (tree rebuilds, many windows); combine this measurement with the code-level caps review.
- Lazy-import / prefetch tradeoff: the candidate defers the uiautomation import to the first tool call (with a background prefetch after initialize). On a zero-idle-gap client (initialize -> tools/list -> cu_show immediately, as in the cold_start section) that first call pays the deferred import (~590ms), so spawn->first cu_show only improves ~10% there; with a realistic idle gap the prefetch finishes and the first call is cheap (verifier's independent measurement: first cu_show 226ms -> 171ms; prefetch matrix after 1.5s idle: 12.2ms with prefetch vs 480.2ms without). Steady-state e2e cu_show is the warm-server metric.
- Candidate snapshot breakdown: the cache-request fast path replaces Bridge._walk / _make_node, so those legacy timers read 0 in candidate runs. Compare wall_ms / ms/visited plus visited/shown node counts (node counts matched the pristine bridge on every target here), not the legacy breakdown.
- e2e numbers are always measured by spawning fresh server processes per side; the first real cu_show of each server must not be isError (CoInitialize/COM-race gate). A single-src baseline doc reports the pristine server; the paired section re-measures both sides.
- GUI collisions on this shared desktop (another agent's test typing into or closing windows, modal dialogs) are handled by fresh-window verification, re-measuring samples slower than 3x the median (outlier retries) and re-launching the controlled Notepad (re-launches); both counters are recorded in the JSON.
- Noise floor: a same-code paired validation run (A and B both pristine, 3 rounds) produced snapshot ms/visited deltas of -5.6%..+1.6% and cold-start adjusted deltas of -3.1%..-1.4%; small-n cold-start noise can reach ~20% (observed in a second n=3 run). Treat deltas inside that band as noise; the AFTER run uses --cmp-rounds 5 and flags anything worse than -10%.
- Candidate first-show gate: in compare mode the harness checks that the candidate's first real cu_show is not isError (CoInitialize/COM-race regression) and exits with code 4 if it is; the JSON is still written for diagnosis. Do not trust e2e/cold-start numbers from a gated run.
- Screenshot is not compared in --compare mode (identical code path); run it in a single-src run against the candidate if screenshot bytes matter.
- Fixtures were generated from the pristine src copy and are not regenerated by ordinary runs (use --dump-fixtures deliberately).

## Reproduce

- baseline: bench

~~~
python bench/opt/bench.py --src-dir D:\opencode\_supercu_opt\orig\src --label baseline --out D:\opencode\plugins\super-cu\bench\opt\baseline.json
~~~

- after: compare

~~~
python bench/opt/bench.py --src-a D:\opencode\_supercu_opt\orig\src --src-b D:\opencode\plugins\super-cu\src --label after --cmp-rounds 5 --sections cold_start,snapshot,render,e2e,memory --out D:\opencode\plugins\super-cu\bench\opt\compare-after.json
~~~

- after-screenshot: bench

~~~
python bench/opt/bench.py --src-dir D:\opencode\plugins\super-cu\src --label after-screenshot --sections screenshot --out D:\opencode\plugins\super-cu\bench\opt\after-screenshot.json --quick
~~~

- Regenerate this report (from the JSON files used above):

~~~
python bench/opt/bench.py --report D:\opencode\plugins\super-cu\bench\opt\baseline.json D:\opencode\plugins\super-cu\bench\opt\compare-after.json D:\opencode\plugins\super-cu\bench\opt\after-screenshot.json --report-out docs/OPT-BASELINE.md
~~~

- AFTER run (authoritative paired evidence once both optimizers are done; interleaves pristine A and candidate B in one process):

~~~
python bench/opt/bench.py --src-a D:/opencode/_supercu_opt/orig/src --src-b D:/opencode/plugins/super-cu/src --label after --cmp-rounds 5 --sections cold_start,snapshot,render,e2e,memory --out bench/opt/compare-after.json
~~~

- Generate the AFTER document (keeps this baseline document immutable):

~~~
python bench/opt/bench.py --report bench/opt/baseline.json bench/opt/compare-after.json --report-out docs/OPT-AFTER.md
~~~

- Optional single-src screenshot numbers for the candidate:

~~~
python bench/opt/bench.py --src-dir D:/opencode/plugins/super-cu/src --label after-screenshot --sections screenshot --out bench/opt/after-screenshot.json --quick
~~~

## Recorded errors

- baseline: none
- after: none
- after-screenshot: none

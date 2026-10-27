# super-cu perf baseline + paired A/B

> **Status**: pristine baseline captured 2026-10-27T09:55:04+0800 (bench/opt/baseline.json). The paired interleaved AFTER run (--src-a pristine --src-b candidate) is pending until both optimizers report complete; the section below is a same-code self-comparison that validates the paired mode. Never compare absolute numbers across different runs on this shared machine.

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

# Paired interleaved A/B (validation run)

- label: **selftest_aa**
- generated_at: 2026-10-27T09:56:37+0800
- schema: 1 (harness 1.0.0)
- quick mode: YES - iteration only, not headline numbers
- rounds: 3 (A and B interleaved, order flips every round)

## Sources

- A (baseline): D:\opencode\_supercu_opt\orig\src
- B (candidate): D:\opencode\_supercu_opt\orig\src

| module | A sha256 (16) | B sha256 (16) | relation |
|---|---|---|---|
| bridge.py | 5974fc395c921f59 | 5974fc395c921f59 | same |
| compact.py | 8d0e328c4f5514fa | 8d0e328c4f5514fa | same |
| dpi.py | 64d1e22390b7add6 | 64d1e22390b7add6 | same |
| gui_lock.py | 479e6a7004c9c398 | 479e6a7004c9c398 | same |
| mock_bridge.py | 4ec91bbde460acbb | 4ec91bbde460acbb | same |
| windows.py | c07bb57a00fbc6a8 | c07bb57a00fbc6a8 | same |

## Machine state (covariates)

| signal | start | end |
|---|---|---|
| captured_at | 2026-10-27T09:56:37+0800 | 2026-10-27T09:57:09+0800 |
| cpu_load_pct | 31.0 | 13.0 |
| python_processes | 9 | 9 |
| visible_windows | 26 | 26 |
| own_rss_mb | 48.2 | 35.38 |
| foreground | DeepSeek Harness: 计算概论终端前端优化 — DeepSeek Harness | DeepSeek Harness: 计算概论终端前端优化 — DeepSeek Harness |

## Controlled window (Notepad)

- window: Notepad #6565448 rect=[67, 60, 1233, 933]
- fresh launch verified: not recorded (captured before the field existed); matched=True, popup check: modal_children=False, other_notepad_windows=0, dismiss_actions=0
- setvalue: ok=True via=setvalue ms=534 verified=True
- content_verified: True (41 lines, sha256=1cc676a54beb6e74)
- rect after window_move: [67, 60, 1233, 933]
- re-launches during run: 0
- cleanup: closed=True forced=False

## Headline (paired medians, B relative to A)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| cold start real adjusted init | 723.5 ms | 647.1 ms | -17.5 ms | -2.2% | 67% |
| cold start real raw init | 995.8 ms | 933.8 ms | -17.5 ms | -1.8% | 67% |
| cold start real spawn->first cu_show | 1007.3 ms | 942.6 ms | -19.5 ms | -1.9% | 67% |
| cold start real init->first cu_show | 11.5 ms | 9.5 ms | -1.9 ms | -17.0% | 67% |
| cold start mock adjusted init | 207.9 ms | 201.6 ms | -6.4 ms | -3.1% | 100% |
| cold start mock raw init | 450.5 ms | 448.9 ms | -6.4 ms | -1.4% | 100% |
| cold start mock spawn->first cu_show | 451.1 ms | 449.3 ms | -6.4 ms | -1.4% | 100% |
| cold start mock init->first cu_show | 0.5 ms | 0.4 ms | -0.1 ms | -21.6% | 67% |
| snapshot notepad_controlled ms/visited | 2.089 | 2.063 | -0.025 | -1.2% | 67% |
| snapshot explorer_system32 ms/visited | 1.883 | 1.745 | -0.118 | -5.6% | 100% |
| snapshot large_real_window ms/visited | 1.159 | 1.181 | 0.019 | 1.6% | 33% |

## Cold start

| mode | A raw init p50 ms | B raw init p50 ms | raw delta % | A adjusted p50 ms | B adjusted p50 ms | adjusted delta % | A ->tools p50 | B ->tools p50 | A ->first show p50 | B ->first show p50 | first show delta % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| real | 995.8 | 933.8 | -1.8% | 723.5 | 647.1 | -2.2% | 996.1 | 934.0 | 1007.3 | 942.6 | -1.9% |
| mock | 450.5 | 448.9 | -1.4% | 207.9 | 201.6 | -3.1% | 450.9 | 449.1 | 451.1 | 449.3 | -1.4% |

## Snapshot (interleaved, controls view)

### notepad_controlled (#6565448 Notepad, read_only=False, outlier retries=0)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| ms/visited | 2.089 | 2.063 | -0.025 | -1.2% | 67% |
| wall ms | 171.27 | 169.20 | -2.07 | -1.2% | 67% |
| bridge elapsed ms | 171.0 | 169.0 | -2.0 | -1.2% | 67% |
| visited nodes | 82 | 82 | 0 | 0.0% | 0% |
| shown nodes | 61 | 61 | 0 | 0.0% | 0% |
| walk ms | 120.75 | 113.56 | 2.56 | 2.4% | 33% |
| make_node ms | 53.97 | 55.21 | 1.68 | 3.1% | 33% |
| control_for ms | 0.00 | 0.00 | 0.00 | 0.0% | 0% |

### explorer_system32 (#185599456 explorer, read_only=False, outlier retries=0)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| ms/visited | 1.883 | 1.745 | -0.118 | -5.6% | 100% |
| wall ms | 240.97 | 223.36 | -15.13 | -5.6% | 100% |
| bridge elapsed ms | 241.0 | 223.0 | -15.0 | -5.6% | 100% |
| visited nodes | 128 | 128 | 0 | 0.0% | 0% |
| shown nodes | 90 | 90 | 0 | 0.0% | 0% |
| walk ms | 192.18 | 179.79 | -12.38 | -6.4% | 100% |
| make_node ms | 55.47 | 52.79 | -2.68 | -4.8% | 67% |
| control_for ms | 0.00 | 0.00 | 0.00 | 0.0% | 0% |

### large_real_window (#54596812 DeepSeek Harness, read_only=True, outlier retries=0)

| metric | A p50 | B p50 | delta p50 | delta % | B faster rounds |
|---|---|---|---|---|---|
| ms/visited | 1.159 | 1.181 | 0.019 | 1.6% | 33% |
| wall ms | 435.62 | 443.93 | 6.96 | 1.6% | 33% |
| bridge elapsed ms | 436.0 | 444.0 | 7.0 | 1.6% | 33% |
| visited nodes | 376 | 376 | 0 | 0.0% | 0% |
| shown nodes | 216 | 216 | 0 | 0.0% | 0% |
| walk ms | 257.50 | 275.03 | 13.59 | 6.2% | 0% |
| make_node ms | 176.47 | 177.42 | 0.68 | 0.4% | 33% |
| control_for ms | 0.00 | 0.00 | 0.00 | 100.0% | 0% |

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
- The e2e numbers above were measured against the PRISTINE server. Re-run them for any candidate build; in particular the first real cu_show after initialize must not return the CoInitialize error (known race in the mid-optimization prefetch implementation, fixed in bridge._ensure_com).
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

- selftest_aa: compare

~~~
python bench/opt/bench.py --src-a D:\opencode\_supercu_opt\orig\src --src-b D:\opencode\_supercu_opt\orig\src --label selftest_aa --out D:\opencode\plugins\super-cu\bench\opt\compare-selftest.json --quick --sections snapshot,cold_start
~~~

- Regenerate this report (from the JSON files used above):

~~~
python bench/opt/bench.py --report D:\opencode\plugins\super-cu\bench\opt\baseline.json D:\opencode\plugins\super-cu\bench\opt\compare-selftest.json --report-out docs/OPT-BASELINE.md
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
- selftest_aa: none

# Phase-B gate (phaseB3)

Generated 2026-10-27T10:35:26. Duration 243.5s.

**11 PASS / 0 FAIL / 0 SKIP / 0 INFO**

## Hashes

| module | pristine | current |
|---|---|---|
| bridge | 5974fc395c921f59 | a9b18446633cd249 |
| server | 44c0bb020f57ad5c | e019e244a81a5e0a |
| compact | 8d0e328c4f5514fa | 8d0e328c4f5514fa |
| windows | c07bb57a00fbc6a8 | c07bb57a00fbc6a8 |
| mock_bridge | 4ec91bbde460acbb | 4ec91bbde460acbb |

## Gates

| id | gate | status | evidence |
|----|------|--------|----------|
| B1 | py_compile all src | PASS | bridge.py; compact.py; dpi.py; gui_lock.py; mock_bridge.py; server.py; windows.py |
| B2 | tests/test_server.py --mock-only | PASS | 197 passed, 0 failed in 2.9s |
| B3 | verify.py run (mock parity + protocol) | PASS | [verify] run phaseB3  base=D:\opencode\_supercu_opt\orig\src  cur=D:\opencode\plugins\super-cu\src |
| B4 | src vs pristine diff review | PASS | bench/verify/out/src_diff_review.md |
| B5 | COM/prefetch race (first real cu_show) | PASS | bench/verify/out/com_race_phaseB3.json |
| B6 | cold start paired A/B (n=7, 2 modes) | PASS | bench/verify/out/coldstart_phaseB3.json |
| B8 | verify.py run --real (snapshot/fallback/screenshot/protocol) | PASS | bench/verify/out/verify_phaseB3-real.json |
| B7 | real-GUI parity + memory + churn + stale-id | PASS | bench/verify/out/realgui_phaseB3.json |
| B9 | tests/test_bridge.py (real GUI, owner run) | PASS | bridge\real_suite_run2.log :: RESULT: 55/55 PASS, 0 FAIL   total 38684ms |
| B10 | profiler AFTER benchmark deltas | PASS | 27 delta rows; after=compare-after.json; fingerprint matches final sources |
| B11 | release version consistency (package.json == SERVER_VERSION) | PASS | package.json='1.2.0' SERVER_VERSION='1.2.0' build_marker='3' |

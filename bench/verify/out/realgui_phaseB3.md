# super-cu real-GUI verification (phaseB3)

Generated 2026-10-27T10:33:34. Notepad/Calculator only, closed afterwards.

**37 PASS / 0 FAIL / 0 SKIP**

| # | check | status | detail |
|---|-------|--------|--------|
| 1 | gui.notepad_open | PASS | hwnd=23335800 |
| 2 | gui.notepad_fill_small | PASS | {"ok": true, "via": "paste", "detail": "粘贴 107 字符（剪贴板已回读校验）", "ms": 138, "changed": true, "verified": null} |
| 3 | parity.orig_vs_cache_small[all] | PASS | identical (75 nodes, total=78 shown=75 truncated=False) |
| 4 | parity.orig_vs_cache_small[controls] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 5 | parity.orig_vs_cache_small[controls#1] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 6 | parity.orig_vs_cache_small[controls#2] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 7 | parity.orig_vs_cache_small[filter=行] | PASS | identical (1 nodes, total=78 shown=1 truncated=False) |
| 8 | parity.orig_vs_cache_small[text] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 9 | parity.cache_vs_legacy_small[all] | PASS | identical except known-volatile scrollbar auto-hide (A/A control: legacy-vs-legacy differs identically): ['node[4](scrollbar/垂直) offscreen: True != False', 'node[4](scrollbar/垂直) rect: [0, 0, 0, 0] != [1209, 173, 1227, 882]'] |
| 10 | parity.cache_vs_legacy_small[controls] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 11 | parity.cache_vs_legacy_small[controls#1] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 12 | parity.cache_vs_legacy_small[controls#2] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 13 | parity.cache_vs_legacy_small[filter=行] | PASS | identical (1 nodes, total=78 shown=1 truncated=False) |
| 14 | parity.cache_vs_legacy_small[text] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 15 | gui.notepad_fill_long | PASS | {"ok": true, "via": "paste", "detail": "粘贴 77863 字符（剪贴板已回读校验）", "ms": 138, "changed": true, "verified": null} |
| 16 | parity.orig_vs_cache_long[all] | PASS | identical (75 nodes, total=78 shown=75 truncated=False) |
| 17 | parity.orig_vs_cache_long[controls] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 18 | parity.orig_vs_cache_long[controls#1] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 19 | parity.orig_vs_cache_long[controls#2] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 20 | parity.orig_vs_cache_long[filter=line] | PASS | identical (0 nodes, total=78 shown=0 truncated=False) |
| 21 | parity.orig_vs_cache_long[text] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 22 | parity.cache_vs_legacy_long[all] | PASS | identical (75 nodes, total=78 shown=75 truncated=False) |
| 23 | parity.cache_vs_legacy_long[controls] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 24 | parity.cache_vs_legacy_long[controls#1] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 25 | parity.cache_vs_legacy_long[controls#2] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 26 | parity.cache_vs_legacy_long[filter=line] | PASS | identical (0 nodes, total=78 shown=0 truncated=False) |
| 27 | parity.cache_vs_legacy_long[text] | PASS | identical (57 nodes, total=78 shown=57 truncated=False) |
| 28 | gui.notepad_closed | PASS | {"action": "close", "hwnd": 23335800, "close": {"ok": true, "via": "win32", "detail": "已关闭", "ms": 562}, "closed": true, "_proc": {"code": 0, "duration_s": 1.73}} |
| 29 | parity.orig_vs_cache_calc[all] | PASS | identical (54 nodes, total=54 shown=54 truncated=False) |
| 30 | parity.orig_vs_cache_calc[controls] | PASS | identical (41 nodes, total=54 shown=41 truncated=False) |
| 31 | parity.orig_vs_cache_calc[controls#1] | PASS | identical (41 nodes, total=54 shown=41 truncated=False) |
| 32 | parity.orig_vs_cache_calc[controls#2] | PASS | identical (41 nodes, total=54 shown=41 truncated=False) |
| 33 | parity.cache_vs_legacy_calc[all] | PASS | identical (54 nodes, total=54 shown=54 truncated=False) |
| 34 | parity.cache_vs_legacy_calc[controls] | PASS | identical (41 nodes, total=54 shown=41 truncated=False) |
| 35 | parity.cache_vs_legacy_calc[controls#1] | PASS | identical (41 nodes, total=54 shown=41 truncated=False) |
| 36 | parity.cache_vs_legacy_calc[controls#2] | PASS | identical (41 nodes, total=54 shown=41 truncated=False) |
| 37 | gui.calculator_closed | PASS | {"action": "close", "hwnd": 8662650, "close": {"ok": true, "via": "win32", "detail": "已关闭", "ms": 562}, "closed": true, "_proc": {"code": 0, "duration_s": 1.79}} |

## Hashes

orig: {"bridge": "5974fc395c921f59", "server": "44c0bb020f57ad5c", "compact": "8d0e328c4f5514fa", "windows": "c07bb57a00fbc6a8", "mock_bridge": "4ec91bbde460acbb"}

cur: {"bridge": "a9b18446633cd249", "server": "e019e244a81a5e0a", "compact": "8d0e328c4f5514fa", "windows": "c07bb57a00fbc6a8", "mock_bridge": "4ec91bbde460acbb"}

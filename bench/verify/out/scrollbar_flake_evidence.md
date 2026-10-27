# Win11 Notepad auto-hide scrollbar - flake evidence (verifier)

Gate B7 realgui compares snapshots taken by SEPARATE processes. Win11
Notepad fades its scrollbar when idle, so the live (legacy) UIA read of a
ScrollBar control alternates between (offscreen=True, rect=[0,0,0,0]) and
(offscreen=False, rect=[1209,173,1227,882]) depending on when the process
happened to sample it. The SAME pristine path produces both states in
different runs, so this is UIA/UI-transition noise, not a cache-path delta.

## Documented snapshot states (from the realgui captures on disk)

| run | content | side(snapshot source) | scrollbar offscreen/rect |
|---|---|---|---|
| phaseB2 | small | base | True [0, 0, 0, 0]; False [1206, 172, 1232, 884] |
| phaseB2 | small | cur | True [0, 0, 0, 0]; False [1206, 172, 1232, 884] |
| phaseB2 | small | cur_nocache | False [1209, 173, 1227, 882]; False [1206, 172, 1232, 884] |
| phaseB2 | long | base | True [0, 0, 0, 0]; False [1206, 172, 1232, 884] |
| phaseB2 | long | cur | True [0, 0, 0, 0]; False [1206, 172, 1232, 884] |
| phaseB2 | long | cur_nocache | False [1209, 173, 1227, 882]; False [1206, 172, 1232, 884] |
| phaseB3 | small | base | True [0, 0, 0, 0]; False [1206, 172, 1232, 884] |
| phaseB3 | small | cur | True [0, 0, 0, 0]; False [1206, 172, 1232, 884] |
| phaseB3 | small | cur_nocache | False [1209, 173, 1227, 882]; False [1206, 172, 1232, 884] |
| phaseB3 | long | base | False [1209, 173, 1227, 882]; False [1206, 172, 1232, 884] |
| phaseB3 | long | cur | False [1209, 173, 1227, 882]; False [1206, 172, 1232, 884] |
| phaseB3 | long | cur_nocache | False [1209, 173, 1227, 882]; False [1206, 172, 1232, 884] |

## A/A control (independent, verifier-run)

Alternating captures of ONE freshly opened Notepad, view=all,
RuntimeId-normalized, focus ignored:

    A/A pristine legacy x2           diffs=0 []
    A/A pristine legacy x2 again     diffs=0 []
    A/A current cache x2             diffs=0 []
    A/A current legacy x2            diffs=2 [scrollbar/垂直 offscreen False != True,
                                              rect [1209,173,1227,882] != [0,0,0,0]]
    A/B pristine vs cache            diffs=0 []
    A/B cache vs legacy              diffs=2 [same scrollbar fields]

=> Even legacy-vs-legacy (same code, two processes) differs on exactly
   these two fields, while cache-vs-cache and pristine-vs-cache are
   deterministic. All non-scrollbar nodes are identical in every run.

## Harness policy adopted

bench/verify/realgui.py treats ONLY scrollbar offscreen/rect divergences
as known-volatile (any other field or any other element still fails the
gate). After this change the full Phase-B gate is 11 PASS / 0 FAIL
(bench/verify/out/phase_b_phaseB3.json).


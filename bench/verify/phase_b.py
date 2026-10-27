#!/usr/bin/env python3
"""Phase-B gate runner (verifier-owned).  Runs the full verification sequence
and writes collected evidence; real-GUI steps open/close their own Notepad (the
caller must own the GUI slot).

    python -X utf8 bench/verify/phase_b.py --tag phaseB
    python -X utf8 bench/verify/phase_b.py --tag phaseB --skip-gui
    python -X utf8 bench/verify/phase_b.py --tag phaseB --hwnds 12345

Writes bench/verify/out/phase_b_<tag>.json + .md with a per-gate PASS/FAIL table.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _vshared as V  # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, "out")
ORIG = r"D:\opencode\_supercu_opt\orig\src"
SRC = os.path.join(REPO, "src")


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _env(extra=None):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def run(name, argv, *, timeout=1800, extra_env=None, cwd=None):
    started = time.perf_counter()
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout,
                              env=_env(extra_env), cwd=cwd or REPO)
        payload = {"name": name, "argv": argv, "code": proc.returncode,
                   "duration_s": round(time.perf_counter() - started, 2),
                   "stdout_tail": proc.stdout.decode("utf-8", "replace")[-6000:],
                   "stderr_tail": proc.stderr.decode("utf-8", "replace")[-3000:]}
    except subprocess.TimeoutExpired:
        payload = {"name": name, "argv": argv, "code": "timeout",
                   "duration_s": round(time.perf_counter() - started, 2),
                   "stdout_tail": "", "stderr_tail": "timeout"}
    print("[phaseB] %-34s code=%s duration=%.1fs"
          % (name, payload["code"], payload["duration_s"]))
    return payload


def gui(args, extra_env=None):
    payload = run("gui:" + " ".join(args), [sys.executable, "-X", "utf8",
                 os.path.join(HERE, "_gui.py"), "--src-dir", ORIG] + args,
                  timeout=300, extra_env=extra_env)
    data = None
    for line in reversed((payload.get("stdout_tail") or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    return data or {}


def read_json(path):
    try:
        return V.read_json(path)
    except Exception:
        return {}


def gate_status(payload, key="code"):
    if payload.get(key) is None:
        return "SKIP"
    return "PASS" if payload[key] == 0 else "FAIL"


def main(argv) -> int:
    _utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--orig-dir", default=ORIG)
    parser.add_argument("--src-dir", default=SRC)
    parser.add_argument("--hwnds", default="")
    parser.add_argument("--skip-gui", action="store_true")
    parser.add_argument("--dialog", action="store_true")
    parser.add_argument("--run-bridge-tests", action="store_true",
                        help="also run the real tests/test_bridge.py (owner "
                             "normally runs it; only with the GUI slot)")
    parser.add_argument("--memory-iters", type=int, default=300)
    args = parser.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    tag = args.tag
    t0 = time.perf_counter()
    gates: list = []
    runs: list = []

    # B1: py_compile
    py_files = sorted(os.path.join(args.src_dir, name)
                      for name in os.listdir(args.src_dir)
                      if name.endswith(".py"))
    compile_run = run("py_compile", [sys.executable, "-m", "py_compile"] + py_files,
                      timeout=300)
    runs.append(compile_run)
    gates.append({"id": "B1", "gate": "py_compile all src",
                  "status": gate_status(compile_run),
                  "evidence": "; ".join(os.path.basename(p) for p in py_files)})

    # B2: mock protocol/unit suite
    mock_run = run("test_server --mock-only",
                   [sys.executable, os.path.join(REPO, "tests",
                                                 "test_server.py"),
                    "--mock-only"], timeout=600, cwd=REPO)
    runs.append(mock_run)
    mock_tail = (mock_run.get("stdout_tail") or "").strip().splitlines()
    gates.append({"id": "B2", "gate": "tests/test_server.py --mock-only",
                  "status": gate_status(mock_run),
                  "evidence": mock_tail[-1] if mock_tail else ""})

    # B3: full verify A/B (mock + protocol) - no GUI
    verify_run = run("verify run", [sys.executable, "-X", "utf8",
                     os.path.join(HERE, "verify.py"), "run",
                     "--orig-dir", args.orig_dir, "--src-dir", args.src_dir,
                     "--tag", tag], timeout=1200)
    runs.append(verify_run)
    gates.append({"id": "B3", "gate": "verify.py run (mock parity + protocol)",
                  "status": gate_status(verify_run),
                  "evidence": verify_run["stdout_tail"].strip().splitlines()[0]
                  if verify_run["stdout_tail"].strip() else ""})

    # B4: static diff review
    diff_run = run("diffreview", [sys.executable, "-X", "utf8",
                   os.path.join(HERE, "diffreview.py"),
                   "--orig", args.orig_dir, "--cur", args.src_dir],
                   timeout=300)
    runs.append(diff_run)
    gates.append({"id": "B4", "gate": "src vs pristine diff review",
                  "status": gate_status(diff_run),
                  "evidence": "bench/verify/out/src_diff_review.md"})

    # B5/B6/B7: real GUI (COM-race, cold start, parity/memory/churn/stale)
    opened = []
    real_hwnds = [x for x in (args.hwnds or "").split(",") if x.strip()]
    if not args.skip_gui:
        if not real_hwnds:
            opened_np = gui(["open", "--app", "notepad"])
            hwnd = int(opened_np.get("hwnd") or 0)
            if hwnd:
                opened.append(("notepad", hwnd))
                real_hwnds = [str(hwnd)]
            else:
                gates.append({"id": "B5", "gate": "open controlled Notepad",
                              "status": "FAIL",
                              "evidence": json.dumps(opened_np,
                                                     ensure_ascii=False)[:300]})
        if real_hwnds:
            hwnds = ",".join(real_hwnds)
            race_run = run("com-race", [sys.executable, "-X", "utf8",
                           os.path.join(HERE, "verify.py"), "com-race",
                           "--src-dir", args.src_dir, "--hwnds", hwnds,
                           "--waits", "0,150,400,800",
                           "--out", os.path.join(OUT, "com_race_%s.json" % tag)],
                           timeout=900)
            runs.append(race_run)
            gates.append({"id": "B5",
                          "gate": "COM/prefetch race (first real cu_show)",
                          "status": gate_status(race_run),
                          "evidence": "bench/verify/out/com_race_%s.json" % tag})
            cold_run = run("coldstart", [sys.executable, "-X", "utf8",
                           os.path.join(HERE, "verify.py"), "coldstart",
                           "--orig-dir", args.orig_dir, "--src-dir",
                           args.src_dir, "--hwnds", hwnds, "--n", "7",
                           "--modes", "prefetch,noprefetch",
                           "--out", os.path.join(OUT,
                                                 "coldstart_%s.json" % tag)],
                           timeout=1800)
            runs.append(cold_run)
            gates.append({"id": "B6",
                          "gate": "cold start paired A/B (n=7, 2 modes)",
                          "status": gate_status(cold_run),
                          "evidence": "bench/verify/out/coldstart_%s.json" % tag})
            # B8: full real capture (read-only) while the controlled window is
            # still open: snapshot parity, cache/legacy fallback, screenshots,
            # real-server protocol (includes the prefetch COM regression).
            rustle = run("verify run --real",
                         [sys.executable, "-X", "utf8",
                          os.path.join(HERE, "verify.py"), "run", "--real",
                          "--orig-dir", args.orig_dir, "--src-dir",
                          args.src_dir, "--hwnds", hwnds, "--iters", "100",
                          "--no-cache-switch", "--tag", tag + "-real"],
                         timeout=2400)
            runs.append(rustle)
            gates.append({"id": "B8",
                          "gate": "verify.py run --real (snapshot/fallback/"
                                  "screenshot/protocol)",
                          "status": gate_status(rustle),
                          "evidence": "bench/verify/out/verify_%s-real.json"
                                      % tag})
            if opened:
                for label, hwnd in opened:
                    close_np = gui(["close", "--hwnd", str(hwnd)])
                    if not close_np.get("closed"):
                        gates.append({"id": "B5b",
                                      "gate": "close controlled %s" % label,
                                      "status": "FAIL",
                                      "evidence": json.dumps(close_np,
                                                             ensure_ascii=False)[:200]})
                opened = []
            realgui_run = run("realgui", [sys.executable, "-X", "utf8",
                              os.path.join(HERE, "realgui.py"),
                              "--tag", tag, "--mem-iters",
                              str(args.memory_iters)]
                              + (["--dialog"] if args.dialog else []),
                              timeout=2400)
            runs.append(realgui_run)
            gates.append({"id": "B7",
                          "gate": "real-GUI parity + memory + churn + stale-id",
                          "status": gate_status(realgui_run),
                          "evidence": "bench/verify/out/realgui_%s.json" % tag})
            realgui = read_json(os.path.join(OUT, "realgui_%s.json" % tag))
            for check in realgui.get("checks") or []:
                if check.get("status") in ("FAIL", "SKIP"):
                    gates.append({"id": "B7",
                                  "gate": "  realgui: " + check["name"],
                                  "status": check["status"],
                                  "evidence": str(check.get("detail"))[:300]})
    else:
        gates.append({"id": "B5-B7", "gate": "real GUI gate",
                      "status": "SKIP", "evidence": "--skip-gui"})

    # B9: real bridge suite (owner normally runs; cite their log, optionally re-run)
    if args.run_bridge_tests and not args.skip_gui:
        bridge_tests = run("test_bridge",
                           [sys.executable, os.path.join(REPO, "tests",
                                                         "test_bridge.py")],
                           timeout=1800, cwd=REPO)
        runs.append(bridge_tests)
        gates.append({"id": "B9", "gate": "tests/test_bridge.py (real GUI)",
                      "status": gate_status(bridge_tests),
                      "evidence": (bridge_tests.get("stdout_tail") or "")
                      .strip().splitlines()[-1] if bridge_tests.get(
                          "stdout_tail", "").strip() else ""})
    else:
        owner = _owner_bridge_suite()
        if owner:
            gates.append({"id": "B9",
                          "gate": "tests/test_bridge.py (real GUI, owner run)",
                          "status": "PASS" if owner["failed"] == 0 else "FAIL",
                          "evidence": "%s :: %s" % (owner["log"],
                                                    owner["line"])})
        else:
            gates.append({"id": "B9",
                          "gate": "tests/test_bridge.py (real GUI)",
                          "status": "SKIP",
                          "evidence": "bridge-opt owner runs it; no owner log "
                                      "found in _supercu_opt/bridge/"})

    # profiler AFTER benchmark artifacts (owner-owned numbers): extract deltas
    # and verify the AFTER run measured the FINAL sources (fingerprint guard).
    delta_run = run("bench_delta", [sys.executable, "-X", "utf8",
                    os.path.join(HERE, "bench_delta.py"),
                    "--src-dir", args.src_dir, "--orig-dir", args.orig_dir],
                    timeout=600)
    runs.append(delta_run)
    delta_json = read_json(os.path.join(OUT, "bench_delta.json"))
    after_path = delta_json.get("after_path") or ""
    after_check = (delta_json.get("fingerprint_check") or {}).get("after") or {}
    base_check = (delta_json.get("fingerprint_check") or {}).get("baseline") or {}
    rows = delta_json.get("rows") or []
    if not after_path:
        gates.append({"id": "B10",
                      "gate": "profiler AFTER benchmark deltas",
                      "status": "SKIP",
                      "evidence": "AFTER benchmark not available yet "
                                  "(profiler pending); baseline fingerprint "
                                  "matches pristine: %r"
                                  % base_check.get("matches_expected")})
    elif after_check and not after_check.get("matches_expected"):
        gates.append({"id": "B10",
                      "gate": "profiler AFTER benchmark deltas",
                      "status": "FAIL",
                      "evidence": "AFTER run measured STALE sources: %s"
                                  % str(after_check.get("mismatched"))[:400]})
    elif rows:
        gates.append({"id": "B10",
                      "gate": "profiler AFTER benchmark deltas",
                      "status": "PASS",
                      "evidence": "%d delta rows; after=%s; fingerprint "
                                  "matches final sources"
                                  % (len(rows),
                                     os.path.basename(after_path))})
    else:
        gates.append({"id": "B10",
                      "gate": "profiler AFTER benchmark deltas",
                      "status": "SKIP",
                      "evidence": "AFTER file found but no comparable rows: %s"
                                  % os.path.basename(after_path)})

    # B11: release version consistency (Lead bumps package.json + SERVER_VERSION
    # together right before the AFTER benchmark; a mismatch is a release risk)
    payload_versions = _versions(args.orig_dir, args.src_dir)
    versions = payload_versions.get("cur") or {}
    package_version = versions.get("package_json")
    server_version = versions.get("server_version")
    if package_version and server_version:
        gates.append({"id": "B11",
                      "gate": "release version consistency "
                              "(package.json == SERVER_VERSION)",
                      "status": "PASS" if package_version == server_version
                                else "FAIL",
                      "evidence": "package.json=%r SERVER_VERSION=%r "
                                  "build_marker=%r"
                                  % (package_version, server_version,
                                     versions.get("cordis_build_marker"))})
    else:
        gates.append({"id": "B11", "gate": "release version consistency",
                      "status": "SKIP", "evidence": "version not readable"})

    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "INFO": 0}
    for gate in gates:
        counts[gate["status"]] = counts.get(gate["status"], 0) + 1
    payload = {"tag": tag, "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "duration_s": round(time.perf_counter() - t0, 1),
               "hashes": {
                   "orig": {name: _hash(os.path.join(args.orig_dir,
                                                    name + ".py"))
                            for name in ("bridge", "server", "compact",
                                         "windows", "mock_bridge")},
                   "cur": {name: _hash(os.path.join(args.src_dir,
                                                   name + ".py"))
                           for name in ("bridge", "server", "compact",
                                        "windows", "mock_bridge")}},
               "versions": payload_versions,
               "gates": gates, "counts": counts, "runs": runs}
    json_path = os.path.join(OUT, "phase_b_%s.json" % tag)
    V.write_json(json_path, payload)
    md_path = os.path.join(OUT, "phase_b_%s.md" % tag)
    lines = ["# Phase-B gate (%s)" % tag, "",
             "Generated %s. Duration %.1fs." % (payload["when"],
                                                payload["duration_s"]),
             "", "**%d PASS / %d FAIL / %d SKIP / %d INFO**"
             % (counts["PASS"], counts["FAIL"], counts["SKIP"],
                counts["INFO"]), "",
             "## Hashes", "",
             "| module | pristine | current |", "|---|---|---|"]
    for name in payload["hashes"]["orig"]:
        lines.append("| %s | %s | %s |"
                     % (name, payload["hashes"]["orig"][name],
                        payload["hashes"]["cur"][name]))
    lines += ["", "## Gates", "", "| id | gate | status | evidence |",
              "|----|------|--------|----------|"]
    for gate in gates:
        lines.append("| %s | %s | %s | %s |"
                     % (gate["id"], gate["gate"], gate["status"],
                        str(gate["evidence"]).replace("|", "/")[:200]))
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print("[phaseB] %d PASS / %d FAIL / %d SKIP / %d INFO"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"], counts["INFO"]))
    for gate in gates:
        if gate["status"] == "FAIL":
            print("  FAIL %s :: %s" % (gate["id"], gate["gate"]))
    print("[phaseB] wrote", json_path)
    print("[phaseB] wrote", md_path)
    return 0 if counts["FAIL"] == 0 else 1


def _read_text_any(path: str) -> str:
    """Read a log written by any tool (utf-8 / utf-16 / PowerShell redirect)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if "RESULT" in text.upper() or "passed" in text.lower():
            return text
    return raw.decode("utf-8", "replace")


def _owner_bridge_suite() -> dict | None:
    """Cite bridge-opt's owner run of tests/test_bridge.py from their log."""
    log_dir = r"D:\opencode\_supercu_opt\bridge"
    if not os.path.isdir(log_dir):
        return None
    candidates = [os.path.join(log_dir, name)
                  for name in os.listdir(log_dir)
                  if name.lower().startswith("real_suite") and
                  name.lower().endswith(".log")]
    if not candidates:
        return None
    candidates.sort(key=os.path.getmtime, reverse=True)
    path = candidates[0]
    text = _read_text_any(path)
    if not text:
        return None
    summary = ""
    for line in text.splitlines():
        if "RESULT" in line.upper():
            summary = line.strip()
    if not summary:
        matches = [line.strip() for line in text.splitlines()
                   if re.search(r"\d+\s*/\s*\d+.*(PASS|passed)", line)]
        summary = matches[-1] if matches else ""
    if not summary:
        return None
    parsed = re.search(r"RESULT:?\s*(\d+)\s*/\s*(\d+)\s*PASS,?\s*"
                       r"(\d+)\s*FAIL", summary, re.IGNORECASE)
    if parsed:
        passed, total, failed = (int(parsed.group(1)), int(parsed.group(2)),
                                 int(parsed.group(3)))
    else:
        passed_matches = re.findall(r"(\d+)\s*(?:passed|PASS)", summary)
        failed_matches = re.findall(r"(\d+)\s*(?:failed|FAIL)", summary)
        passed = int(passed_matches[-1]) if passed_matches else None
        failed = int(failed_matches[-1]) if failed_matches else 0
        total = None
    return {"log": os.path.relpath(path,
                                   r"D:\opencode\_supercu_opt"),
            "line": summary[:220],
            "passed": passed, "failed": failed, "total": total}


def _versions(orig_dir, cur_dir) -> dict:
    """Release-version fingerprint: package.json + SERVER_VERSION per side."""
    out = {}
    for label, src_dir in (("orig", orig_dir), ("cur", cur_dir)):
        root = os.path.dirname(os.path.abspath(src_dir))  # plugin root
        package = {}
        try:
            with open(os.path.join(root, "package.json"), "r",
                      encoding="utf-8") as fh:
                package = json.load(fh)
        except Exception:
            package = {}
        server_version = ""
        try:
            with open(os.path.join(src_dir, "server.py"), "r",
                      encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("SERVER_VERSION"):
                        server_version = line.split("=", 1)[1].strip().strip("\"'")
                        break
        except OSError:
            pass
        build_marker = ""
        try:
            with open(os.path.join(root, "cordis.patch.yml"), "r",
                      encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped.startswith("SUPER_CU_BUILD:"):
                        build_marker = stripped.split(":", 1)[1].strip().strip("\"'")
                        break
        except OSError:
            pass
        out[label] = {"package_json": package.get("version"),
                      "server_version": server_version,
                      "cordis_build_marker": build_marker,
                      "package_json_sha": _hash(os.path.join(root,
                                                             "package.json")),
                      "cordis_sha": _hash(os.path.join(root,
                                                       "cordis.patch.yml"))}
    return out


def _hash(path):
    try:
        import hashlib
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return "-"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""
run_claude.py — CLI runner for FlakyDoctor's Claude repair.

Presents a simple batch/pass@k command surface:

    python3 runner/run_claude.py <container> --runs 1 --models claude

It drives FlakyDoctor's existing container pipeline
(docker/run_in_container.sh -> run_af_fd.py / run_af_fd_id.py -> flakydoctor.py).

NOTE: this is a plain CLI runner, not an agent — FlakyDoctor calls the Anthropic
API directly inside its neuro-symbolic repair loop. FlakyDoctor repairs ID, OD and
NIO — TD containers are not supported.

- Reads FlakyDoctor/test_config.csv, dispatches by test type.
- Runs the repair once per --runs, archiving each to
  FlakyDoctor/data/<container>/run_<NN>/ with meta.json + a verdict.
- Model aliases (config.CLAUDE_MODELS) reach the repair loop via FD_CLAUDE_MODEL.
- Key: ANTHROPIC_API_KEY env wins, else FlakyDoctor/.anthropic_api_key.
  Use --reproduce-only for a free, no-key reproduction (no repair).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FLAKYDOCTOR_DIR = SCRIPT_DIR.parent
CSV_FILE = FLAKYDOCTOR_DIR / "test_config.csv"
RUN_IN_CONTAINER = FLAKYDOCTOR_DIR / "docker" / "run_in_container.sh"
OUTPUTS_DIR = FLAKYDOCTOR_DIR / "outputs"
DATA_DIR = FLAKYDOCTOR_DIR / "data"
KEY_FILE = FLAKYDOCTOR_DIR / ".anthropic_api_key"

sys.path.insert(0, str(SCRIPT_DIR))
import config  # noqa: E402

SUPPORTED_TYPES = {"od", "id", "nio"}


def log(msg: str) -> None:
    print(f"[run_claude] {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"[run_claude] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# --------------------------------------------------------------------------- config

def resolve_model(alias: str) -> str:
    key = alias.strip().lower()
    if key in config.CLAUDE_MODELS:
        return config.CLAUDE_MODELS[key]
    if key.startswith("claude"):
        return alias  # a full claude model id, passed through unchanged
    die(f"unsupported model '{alias}'. This runner is Claude-only; aliases: "
        f"{', '.join(sorted(config.CLAUDE_MODELS))}. "
        f"(FlakyDoctor also supports GPT-4, but only via src/flakydoctor.py --model GPT-4.)")
    return ""  # unreachable


def load_row(container: str) -> dict | None:
    if not CSV_FILE.is_file():
        die(f"test_config.csv not found at {CSV_FILE}")
    with open(CSV_FILE, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("result_container") or "").strip() == container:
                return row
    return None


def resolve_key(reproduce_only: bool) -> str:
    val = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not val and KEY_FILE.is_file():
        val = KEY_FILE.read_text().strip()
    if not val:
        if reproduce_only:
            return "unused"  # run_in_container.sh needs a key; --skip-repair never uses it
        die("no Anthropic key found. Set ANTHROPIC_API_KEY or create FlakyDoctor/.anthropic_api_key "
            "(or pass --reproduce-only for a free, no-key reproduction).")
    return val


# ---------------------------------------------------------------------------- runs

def next_run_dir(container: str) -> tuple[Path, int]:
    base = DATA_DIR / container
    base.mkdir(parents=True, exist_ok=True)
    nums = [int(p.name.split("_", 1)[1]) for p in base.glob("run_*")
            if p.is_dir() and p.name.split("_", 1)[1].isdigit()]
    idx = (max(nums) + 1) if nums else 1
    run_dir = base / f"run_{idx:02d}"
    run_dir.mkdir()
    return run_dir, idx


def verdict_from_results(fd_out_dir: Path) -> str:
    """PASSED if any round reached test_pass; FAILED if the loop finished without one;
    INCOMPLETE if FlakyDoctor errored or produced no verdict."""
    rj = fd_out_dir / "results.json"
    try:
        raw = rj.read_text().strip()
    except OSError:
        return "INCOMPLETE"
    if not raw:
        return "INCOMPLETE"
    dec, i, tool_failed = json.JSONDecoder(), 0, False
    try:
        while i < len(raw):
            obj, end = dec.raw_decode(raw, i)
            i = end
            while i < len(raw) and raw[i] in " \n\t,":
                i += 1
            if any(v == "test_pass" for v in (obj.get("test_results") or {}).values()):
                return "PASSED"
            if obj.get("Exceptions"):
                tool_failed = True
    except ValueError:
        return "INCOMPLETE"
    return "INCOMPLETE" if tool_failed else "FAILED"


def run_once(container: str, test_type: str, model_alias: str, model_id: str,
             reproduce_only: bool, key: str, keep_m2: bool = False) -> dict:
    run_dir, run_idx = next_run_dir(container)
    log(f"container={container} type={test_type} model={model_alias}({model_id}) "
        f"run={run_idx}{' [reproduce-only]' if reproduce_only else ''} -> {run_dir}")

    env = dict(os.environ)
    env["FD_CLAUDE_MODEL"] = model_id
    env["ANTHROPIC_API_KEY"] = key
    if keep_m2:
        # keep the offline .m2 AND the staged source/build between pass@k runs;
        # both are cleaned up on the final run of the batch.
        env["KEEP_FLAKY_M2"] = "1"
        env["KEEP_SOURCE"] = "1"

    cmd = ["bash", str(RUN_IN_CONTAINER), container]
    if reproduce_only:
        cmd.append("--skip-repair")

    # Snapshot FlakyDoctor's own output dirs so we can identify the one this run creates
    # (run_af_fd.py names it outputs/af_fd_<zip>_<ts>, keyed on the zip, not the container).
    before = set(glob.glob(str(OUTPUTS_DIR / "af_fd_*")))

    t0 = time.time()
    with open(run_dir / "pipeline.log", "w") as lf:
        proc = subprocess.Popen(cmd, cwd=str(FLAKYDOCTOR_DIR), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            lf.write(line)
        proc.wait()
    elapsed = time.time() - t0
    rc = proc.returncode

    # Import FlakyDoctor's per-run output dir (repair mode only; --skip-repair makes none).
    fd_out_dir = None
    new = sorted(set(glob.glob(str(OUTPUTS_DIR / "af_fd_*"))) - before, key=os.path.getmtime)
    if new:
        dst = run_dir / "flakydoctor_output"
        shutil.move(new[-1], str(dst))
        fd_out_dir = dst

    if reproduce_only:
        verdict = "REPRODUCED" if rc == 0 else "INCOMPLETE"
    elif fd_out_dir is not None:
        verdict = verdict_from_results(fd_out_dir)
    else:
        verdict = "INCOMPLETE"

    meta = {
        "container": container,
        "test_type": test_type,
        "model_alias": model_alias,
        "model_id": model_id,
        "run_index": run_idx,
        "reproduce_only": reproduce_only,
        "verdict": verdict,
        "return_code": rc,
        "elapsed_s": round(elapsed, 1),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "flakydoctor_output": str(fd_out_dir.relative_to(FLAKYDOCTOR_DIR)) if fd_out_dir else None,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (run_dir / ".run_complete").write_text("")
    log(f"run {run_idx}: verdict={verdict} ({elapsed:.0f}s) -> {run_dir}")
    return meta


SUMMARY_FIELDS = ["container", "test_type", "model_alias", "model_id", "run_index",
                  "reproduce_only", "verdict", "return_code",
                  "elapsed_s", "timestamp_utc"]


def append_summary(container: str, metas: list[dict]) -> None:
    for path in (DATA_DIR / container / "summary.csv", DATA_DIR / "Complete_Containers_Summary.csv"):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
            if write_header:
                w.writeheader()
            for m in metas:
                w.writerow({k: m.get(k) for k in SUMMARY_FIELDS})


# ---------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(
        description="CLI runner for FlakyDoctor's Claude repair (ID/OD/NIO only).")
    ap.add_argument("container", help="result_container name from test_config.csv")
    ap.add_argument("--models", default="claude",
                    help="comma-separated Claude model aliases/ids (default: claude)")
    ap.add_argument("--runs", type=int, default=1,
                    help="independent runs per model for pass@k (default 1)")
    ap.add_argument("--reproduce-only", action="store_true",
                    help="reproduce the flake and stop (no repair, no API cost, no key needed)")
    args = ap.parse_args()

    if not RUN_IN_CONTAINER.is_file():
        die(f"missing {RUN_IN_CONTAINER}")
    row = load_row(args.container)
    if not row:
        die(f"container '{args.container}' not found in {CSV_FILE.name}")
    test_type = (row.get("test_type") or "").strip().lower()
    if test_type not in SUPPORTED_TYPES:
        die(f"test type '{test_type}' is not supported by FlakyDoctor (only ID, OD and NIO). "
            f"TD/other containers cannot be repaired here.")
    if args.runs < 1:
        die("--runs must be >= 1")

    key = resolve_key(args.reproduce_only)
    resolved = [(m.strip(), resolve_model(m)) for m in args.models.split(",") if m.strip()]
    if not resolved:
        die("no models given")

    metas: list[dict] = []
    total = len(resolved) * args.runs
    done = 0
    for alias, model_id in resolved:
        for _ in range(args.runs):
            done += 1
            metas.append(run_once(args.container, test_type, alias, model_id,
                                  args.reproduce_only, key, keep_m2=done < total))
    append_summary(args.container, metas)

    log("=" * 60)
    for m in metas:
        log(f"run {m['run_index']:>2}  {m['model_alias']:<12} {m['verdict']}")
    if not args.reproduce_only:
        passed = sum(1 for m in metas if m["verdict"] == "PASSED")
        log(f"pass@{args.runs}: {passed}/{len(metas)} run(s) PASSED")
    log(f"artifacts under {DATA_DIR / args.container}")


if __name__ == "__main__":
    main()

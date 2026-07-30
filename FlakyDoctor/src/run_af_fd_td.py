#!/usr/bin/env python3
"""
run_af_fd_td.py — run an AgentFlake TD container end-to-end through FlakyDoctor + Claude.

The TD (Test/Timing-Dependent) analog of run_af_fd_id.py. It reuses run_af_fd's staging /
build / maven helpers verbatim, but swaps the ID-specific parts for TD:

  1. select the `td` row from test_config.csv (by result_container name)
  2. stage the Zenodo zip into projects/<zipbase>/<project>/  (reuses run_af_fd), which now
     also stages the FlakyCodeChange.patch timing forcing next to Fixed.patch
  3. git-init a baseline commit                               (reuses run_af_fd)
  4. build with the container's pre-staged .m2                (reuses run_af_fd)
  5. DETERMINISTICALLY reproduce the TD flake by applying the FlakyCodeChange forcing on top of
     the pristine tree and running the plain victim; gated on the victim FAILING. The forcing is
     reverted immediately after, so FlakyDoctor starts from a pristine tree. Zero API cost.
  6. run flakydoctor.py --flakiness-type TD --model Claude    (unless --skip-repair)
  7. summarize.

A TD test passes when run on its own on the pristine tree; it fails only under the timing
forcing (a perturbation, e.g. an injected Thread.sleep, that makes the latent timing flake
deterministic). There is no polluter (OD), no NonDex seed (ID) and no wrapper (NIO). The victim
test method is the only repair target; verification runs it WITH the forcing applied (a real fix
passes even under the forcing, an empty patch still fails) — see parse_nondex.run_test_with_td.

Usage (from the FlakyDoctor root):
  python3 src/run_af_fd_td.py \
      --container BOOKKEEPER-846 \
      --api-key "$(cat .anthropic_api_key)"

  python3 src/run_af_fd_td.py --list
  python3 src/run_af_fd_td.py --container X --skip-repair   # reproduce only
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time

import run_af_fd as rf  # reuse stage_container/build_project/maven_env/ensure_git_baseline/...


# ---------------------------------------------------------------- row parsing

def load_td_rows(test_config):
    """All `td` rows. Columns (0-indexed): test=line[5], module=line[3], iterations=line[6],
    java=line[8], url=line[10]. Like NIO there is no polluter (col 4) and no nondexSeed (col 9);
    the reproduction condition is the staged FlakyCodeChange.patch timing forcing."""
    rows = []
    with open(test_config, newline="") as f:
        for line in csv.reader(f):
            if len(line) < 11 or line[0].strip().lower() != "td":
                continue
            rows.append({
                "container": line[1].strip(),
                "zip_id": line[2].strip(),
                "module": line[3].strip() or ".",
                "test": line[5].strip(),        # Class#method (the single flaky test)
                "iterations": line[6].strip() or "10",
                "java": line[8].strip() or "8",
                "url": line[10].strip(),
            })
    return rows


# --------------------------------------------------------------- forcing patch

def _forcing_patch(container_dir):
    """The FlakyCodeChange timing forcing staged next to Fixed.patch (absolute path so it survives
    run_td.sh's cd into the project)."""
    return os.path.join(os.path.abspath(container_dir), "FlakyCodeChange.patch")


# ------------------------------------------------------- reproduce (forcing)

def td_test_result(output):
    """Mirror parse_nondex.analyze_td_test_result. The victim runs alone under the forcing; a
    @Test(timeout=) tripped by the forcing makes JUnit double-count the same victim (timeout +
    the still-running thread's own exception), e.g. 'Tests run: 2, Errors: 2', so classify each
    Surefire totals line by failures+errors rather than an exact test count."""
    results = []
    for line in output.split("\n"):
        m = re.search(r"Tests run: \d+, Failures: (\d+), Errors: (\d+)", line)
        if m:
            results.append("test_pass" if int(m.group(1)) + int(m.group(2)) == 0 else "test_failure")
    if results:
        return "test_pass" if "test_failure" not in results else "test_failure"
    if "COMPILATION ERROR" in output:
        return "compilation_error"
    return "build_failure"


def reproduce_with_forcing(container_dir, project_dir, module, victim, jdk):
    """Reproduce through the same run_td.sh the repair engine verifies with: it applies the
    FlakyCodeChange timing forcing, runs the plain victim, then reverts the forcing. Gate: the
    victim must FAIL under the forcing (the TD flake reproduces deterministically). Zero API cost."""
    forcing = _forcing_patch(container_dir)
    if not os.path.exists(forcing):
        rf.die(f"FlakyCodeChange.patch not found at {forcing} — a TD zip must ship the timing "
               "forcing; cannot reproduce the TD flake")
    rf.log(f"reproducing under the FlakyCodeChange timing forcing (victim run on its own): {victim}")
    env = rf.maven_env(container_dir, jdk)
    try:
        res = subprocess.run(["bash", "src/cmds/run_td.sh", project_dir, module, victim, jdk, forcing],
                             env=env, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        rf.die("TD victim run timed out after 30 min under the forcing")
    out = res.stdout + ("\n" + res.stderr if res.stderr else "")
    if "forcing did not apply" in out:
        rf.die("the FlakyCodeChange forcing could not be applied to the pristine tree — is `patch` "
               "installed in the image? — cannot reproduce the TD flake (not a real failure)")
    if "BUILD FAILURE" in out and "Tests run:" not in out:
        rf.die("could not build/run the victim under the forcing (last lines):\n"
               + "\n".join(out.splitlines()[-15:]))
    result = td_test_result(out)
    rf.log(f"  result: {result}")
    if result == "test_failure":
        rf.log("TD FLAKE REPRODUCED — the victim fails deterministically under the timing forcing")
        return
    if result == "test_pass":
        rf.die("the victim PASSED even under the forcing (the timing flake does not reproduce "
               "in this environment) — not spending API calls.")
    rf.die(f"unexpected victim result '{result}' — check the build (last lines):\n"
           + "\n".join(out.splitlines()[-15:]))


# ------------------------------------------------------------------- repair

def run_flakydoctor_td(container_dir, row, github_url, project_name, projects_dir,
                       api_key, model, jdk):
    zip_base = os.path.basename(container_dir)
    _, test_dotted = rf.split_test(row["test"])
    url = github_url or f"https://github.com/agentflake/{project_name}"
    if url.rstrip("/").split("/")[-1] != project_name:
        rf.die(f"staged dir name '{project_name}' must equal the URL's last segment ({url})")

    out_dir = os.path.join("outputs", f"af_fd_td_{zip_base}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(out_dir, exist_ok=True)
    input_csv = os.path.join(out_dir, "input.csv")
    # repair_TD input format: project,sha,module,test,test_type,status,pr,notes
    with open(input_csv, "w") as f:
        f.write(f"{url},{zip_base},{row['module']},{test_dotted},TD,,,\n")

    env = rf.maven_env(container_dir, jdk)
    cmd = ["python3", "-u", "src/flakydoctor.py",
           "--input-tests-csv", input_csv,
           "--flakiness-type", "TD",
           "--projects", projects_dir,
           "--api-key", api_key,
           "--model", model,
           "--output-dir", out_dir,
           "--output-result-csv", os.path.join(out_dir, "results.csv"),
           "--output-result-json", os.path.join(out_dir, "results.json"),
           "--output-details-json", os.path.join(out_dir, "details.json")]
    rf.log(f"running FlakyDoctor ({model}, TD); live output follows, artifacts in {out_dir}/")
    rf.clean_m2_markers(container_dir)  # clear cached remote-failure markers before verify
    rf.run_flakydoctor_cmd(cmd, env, out_dir)
    return out_dir


def summarize_td(out_dir, container_dir):
    results = os.path.join(out_dir, "results.json")
    if not os.path.exists(results):
        rf.log("no results.json produced — check the run output above")
        return
    raw = open(results).read().strip()
    if not raw:
        rf.log("results.json is empty — FlakyDoctor produced no verdict (check output above)")
        return
    dec, idx = json.JSONDecoder(), 0
    while idx < len(raw):
        obj, end = dec.raw_decode(raw, idx)
        idx = end
        while idx < len(raw) and raw[idx] in " \n\t,":
            idx += 1
        tr = obj.get("test_results", {})
        fixed = any(v == "test_pass" for v in tr.values())
        print()
        rf.log(f"test    : {obj.get('victim') or obj.get('test')}")
        rf.log(f"rounds  : {tr}")
        rf.log(f"fixed   : {'YES' if fixed else 'NO'}")
        rf.log(f"patch   : {obj.get('patch_file')}")
        if obj.get("Exceptions"):
            rf.log(f"exceptions: {obj['Exceptions']}")
    dev_fix = os.path.join(container_dir, "Fixed.patch")
    if os.path.exists(dev_fix):
        rf.log(f"developer's reference fix for comparison: {dev_fix}")


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test-config", default="test_config.csv", help="path to test_config.csv (default: test_config.csv in the FlakyDoctor root)")
    ap.add_argument("--container", help="result_container name (col 2) of the td row to run")
    ap.add_argument("--list", action="store_true", help="list td rows")
    ap.add_argument("--api-key", help="Anthropic API key (required unless --skip-repair)")
    ap.add_argument("--model", default="Claude", help="FlakyDoctor model (default: Claude)")
    ap.add_argument("--projects", default="projects", help="FlakyDoctor projects dir")
    ap.add_argument("--skip-repair", action="store_true", help="stop after reproducing (zero API cost)")
    ap.add_argument("--keep-zip", action="store_true", help="keep the downloaded zip in /tmp")
    ap.add_argument("--fresh", action="store_true",
                    help="remove any existing staged container and re-download + rebuild "
                         "from pristine source (use when re-running an already-repaired container)")
    ap.add_argument("--strip-snapshot", action="store_true",
                    help="rewrite <version>X-SNAPSHOT</version> -> X in every pom.xml after "
                         "staging, so inter-module deps resolve against the RELEASE artifacts "
                         "cached in the staged .m2")
    args = ap.parse_args()

    if not os.path.exists("src/flakydoctor.py"):
        rf.die("run this from the FlakyDoctor root (src/flakydoctor.py not found)")

    rows = load_td_rows(args.test_config)
    if not rows:
        rf.die(f"no td rows found in {args.test_config}")

    if args.list:
        print(f"{'container':55} java  test")
        for r in rows:
            print(f"{r['container']:55} {r['java']:5} {r['test']}")
        return

    if not args.container:
        rf.die("--container is required (use --list to see options)")
    matches = [r for r in rows if r["container"] == args.container]
    if not matches:
        rf.die(f"no td row with result_container == {args.container}")
    row = matches[0]

    if not args.skip_repair and not args.api_key:
        rf.die("--api-key is required (or pass --skip-repair to stop after reproduction)")

    container_dir, project_dir, project_name, github_url = \
        rf.stage_container(row, args.projects, keep_zip=args.keep_zip, fresh=args.fresh)
    rf.ensure_git_baseline(project_dir)
    if args.strip_snapshot:
        # after the baseline so the rewrite is committed (survives FlakyDoctor's git stash)
        rf.strip_snapshot_versions(project_dir)
    jdk = rf.build_project(container_dir, project_dir, row["module"], row["java"])
    reproduce_with_forcing(container_dir, project_dir, row["module"], row["test"], jdk)

    if args.skip_repair:
        rf.log("--skip-repair: stopping after successful reproduction.")
        return

    out_dir = run_flakydoctor_td(container_dir, row, github_url, project_name,
                                 args.projects, args.api_key, args.model, jdk)
    summarize_td(out_dir, container_dir)
    rf.generate_semantic_diff(out_dir)
    rf.remove_flaky_m2(container_dir)
    rf.prune_container_dir(container_dir)


if __name__ == "__main__":
    main()

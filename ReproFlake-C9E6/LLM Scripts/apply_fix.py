#!/usr/bin/env python3
"""
apply_fix.py

Apply an llm_response.json fix to the Flaky/ source tree of a result container.

Pipeline (stops at first success):
    1. git apply -p1                  (strict)
    2. git apply -p1 --recount        (tolerates wrong @@ counts)
    3. Splice output_b.fixed_code     (uses operation/anchor schema)

After a successful apply:
    - host-side `javac` smoke-tests each touched .java file (syntax check)
    - container-side `mvn test-compile` regenerates bytecode in target/
      so downstream surefire runs don't read stale .class files
The applier modifies Flaky/ in-place and writes a JSON report to
Steps Output Files/apply_report.json.

Usage:
    python apply_fix.py <result_container> [--no-verify] [--no-recompile]
                                           [--docker-container NAME] [--dry-run]

The Flaky/ directory does NOT need to be a git repository — git apply
operates on plain unified diffs without an index when invoked with --check
or against a working tree.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"


# ---------------------------------------------------------------------------
# Layer 1 & 2: unified-diff applier (output_a)
# ---------------------------------------------------------------------------

def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def apply_patch(flaky_root: Path, patch_text: str) -> dict:
    """Try `git apply -p1`, then `git apply -p1 --recount`. Return the first
    layer that lands or a failure record."""
    if not patch_text or not patch_text.strip():
        return {"layer": None, "ok": False, "reason": "empty patch"}

    patch_text = _ensure_trailing_newline(patch_text)

    layers = [
        ("git apply",            ["git", "apply", "-p1"]),
        ("git apply --recount",  ["git", "apply", "-p1", "--recount"]),
    ]

    last_err = None
    for name, cmd in layers:
        check = subprocess.run(
            cmd + ["--check"],
            input=patch_text, text=True, cwd=flaky_root,
            capture_output=True,
        )
        if check.returncode != 0:
            last_err = check.stderr.strip()
            continue
        apply = subprocess.run(
            cmd,
            input=patch_text, text=True, cwd=flaky_root,
            capture_output=True,
        )
        if apply.returncode == 0:
            return {"layer": name, "ok": True}
        last_err = apply.stderr.strip()

    return {"layer": None, "ok": False, "reason": last_err or "all patch layers rejected"}


def check_patch(flaky_root: Path, patch_text: str) -> dict:
    """Dry-run variant: report which layer would land, without modifying files."""
    if not patch_text:
        return {"layer": None, "ok": False, "reason": "empty patch"}
    patch_text = _ensure_trailing_newline(patch_text)
    for name, cmd in [
        ("git apply",           ["git", "apply", "-p1", "--check"]),
        ("git apply --recount", ["git", "apply", "-p1", "--recount", "--check"]),
    ]:
        r = subprocess.run(cmd, input=patch_text, text=True,
                           cwd=flaky_root, capture_output=True)
        if r.returncode == 0:
            return {"layer": name, "ok": True}
    return {"layer": None, "ok": False, "reason": "all patch layers rejected"}


# ---------------------------------------------------------------------------
# Layer 3: structured splicer (output_b.fixed_code)
# ---------------------------------------------------------------------------

# Java method signature pattern: leading whitespace, optional modifiers,
# return type, name, parameter list, optional throws, opening brace.
# Modifiers and return type intentionally permissive — we don't validate
# Java; we just want to find the line where `<name>(...)` is declared.
_METHOD_PATTERN_TMPL = (
    r'^([ \t]*)'                                            # leading indent
    r'(?:(?:public|private|protected|static|final|'
    r'synchronized|abstract|default|native)\s+)*'           # modifiers
    r'[\w<>\[\]\?,\s\.]+\s+'                                # return type
    r'{name}\s*\([^)]*\)\s*'                                # name(args)
    r'(?:throws\s[^{{]+)?\{{'                               # optional throws + brace
)


def parse_imports_field(imports_field) -> list:
    """Normalise the @@IMPORTS field to a list of `import X;` strings."""
    if not imports_field:
        return []
    if isinstance(imports_field, list):
        lines = imports_field
    else:
        lines = imports_field.split("\n")
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if not ln.startswith("import "):
            ln = "import " + ln
        if not ln.endswith(";"):
            ln += ";"
        out.append(ln)
    return out


def add_imports(src: str, new_imports: list) -> str:
    """Add imports that aren't already present, after the last existing
    import statement. Idempotent: re-running adds nothing."""
    if not new_imports:
        return src

    have = set()
    for m in re.finditer(r'^\s*(import\s+(?:static\s+)?[\w\.\*]+\s*;)', src, re.M):
        have.add(re.sub(r'\s+', ' ', m.group(1)).strip())

    to_add = []
    for imp in new_imports:
        norm = re.sub(r'\s+', ' ', imp).strip()
        if norm not in have:
            to_add.append(imp)
            have.add(norm)
    if not to_add:
        return src

    last_import = None
    for m in re.finditer(r'^\s*import\s+[^;]+;\s*$', src, re.M):
        last_import = m
    if last_import:
        insert_at = last_import.end()
        return src[:insert_at] + "\n" + "\n".join(to_add) + src[insert_at:]

    pkg = re.search(r'^\s*package\s+[^;]+;\s*$', src, re.M)
    if pkg:
        return src[:pkg.end()] + "\n\n" + "\n".join(to_add) + src[pkg.end():]
    return "\n".join(to_add) + "\n\n" + src


def find_method(src: str, name: str):
    """Locate a method by name. Returns (head, end) byte offsets including
    leading annotation lines, or None.

    Limitations: matches the FIRST method with this name (no overload
    disambiguation), and assumes a top-level method in the outermost class.
    """
    pat = re.compile(_METHOD_PATTERN_TMPL.format(name=re.escape(name)), re.M)
    m = pat.search(src)
    if not m:
        return None
    body_brace = m.end() - 1
    head = m.start()

    # Walk backwards, swallowing leading @Annotation lines (and any blank
    # lines between consecutive annotations).
    pre_lines = src[:head].splitlines(keepends=True)
    while pre_lines:
        last = pre_lines[-1]
        stripped = last.lstrip()
        if stripped.startswith("@"):
            head -= len(last)
            pre_lines.pop()
        elif last.strip() == "" and len(pre_lines) >= 2 \
                and pre_lines[-2].lstrip().startswith("@"):
            head -= len(last)
            pre_lines.pop()
        else:
            break

    # Brace-balance forward from the opening brace.
    depth = 0
    i = body_brace
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                if end < len(src) and src[end] == "\n":
                    end += 1
                return (head, end)
        i += 1
    return None


def find_outer_class_close(src: str):
    """Return the offset of the outermost class's closing '}', or None."""
    depth = 0
    in_class = False
    last = -1
    for i, c in enumerate(src):
        if c == "{":
            in_class = True
            depth += 1
        elif c == "}" and in_class:
            depth -= 1
            if depth == 0:
                last = i
    return last if last >= 0 else None


def get_indent_at(src: str, offset: int) -> str:
    """Return the leading whitespace of the line containing `offset`."""
    line_start = src.rfind("\n", 0, offset) + 1
    line_end = src.find("\n", line_start)
    if line_end == -1:
        line_end = len(src)
    line = src[line_start:line_end]
    return re.match(r'^[ \t]*', line).group(0)


def reindent_first_line(code: str, indent: str) -> str:
    """If the first line of `code` is non-empty and lacks `indent`, prepend it.
    Subsequent lines are left alone (they typically already carry the class's
    indent baked in by the model)."""
    lines = code.split("\n")
    if lines and lines[0].strip() and not lines[0].startswith(indent):
        lines[0] = indent + lines[0]
    return "\n".join(lines)


def parse_anchor(anchor: str):
    """Parse @@ANCHOR value. Returns (kind, target_or_none) or None."""
    if not anchor:
        return None
    s = anchor.strip()
    m = re.match(r'(before_method|after_method)\s*=\s*(\w+)\s*$', s)
    if m:
        return (m.group(1), m.group(2))
    if s == "end_of_class":
        return ("end_of_class", None)
    return None


def apply_fixed_code_entry(src: str, entry: dict) -> tuple:
    """Apply ONE fixed_code entry to file content. Returns (new_src, info_dict).
    Raises ValueError on schema violations or unfindable anchors."""
    info = {
        "file": entry.get("file"),
        "method": entry.get("method"),
        "operation": entry.get("operation") or "replace_method",
    }

    # 1. Add imports
    imports = parse_imports_field(entry.get("imports"))
    src = add_imports(src, imports)
    info["imports_added"] = len(imports)

    # 2. Method splice
    operation = info["operation"]
    method_name = entry.get("method")
    code = entry.get("code") or ""

    if not method_name or not code:
        raise ValueError(f"missing method or code field: {entry}")

    if operation == "replace_method":
        loc = find_method(src, method_name)
        if loc is None:
            raise ValueError(
                f"replace_method: method {method_name!r} not found in {info['file']}")
        indent = get_indent_at(src, loc[0])
        new_code = reindent_first_line(code, indent)
        if not new_code.endswith("\n"):
            new_code += "\n"
        return src[:loc[0]] + new_code + src[loc[1]:], info

    if operation == "insert_method":
        if find_method(src, method_name) is not None:
            raise ValueError(
                f"insert_method: a method named {method_name!r} already exists "
                f"in {info['file']} — operation contradicts file state")

        anchor = parse_anchor(entry.get("anchor"))
        if anchor is None:
            info["anchor_warning"] = "missing or invalid anchor; defaulting to end_of_class"
            anchor = ("end_of_class", None)
        kind, target = anchor

        if kind in ("before_method", "after_method"):
            loc = find_method(src, target)
            if loc is None:
                raise ValueError(
                    f"insert_method: anchor target {target!r} not found in {info['file']}")
            indent = get_indent_at(src, loc[0])
            new_code = reindent_first_line(code, indent)
            if kind == "before_method":
                return src[:loc[0]] + new_code + "\n\n" + src[loc[0]:], info
            else:
                # loc[1] already includes a trailing newline
                return src[:loc[1]] + "\n" + new_code + "\n" + src[loc[1]:], info

        if kind == "end_of_class":
            close = find_outer_class_close(src)
            if close is None:
                raise ValueError(f"end_of_class: outer class brace not found in {info['file']}")
            new_code = reindent_first_line(code, "    ")
            return src[:close] + "\n" + new_code + "\n" + src[close:], info

    raise ValueError(f"unknown operation: {operation!r}")


def apply_fixed_code(flaky_root: Path, entries: list) -> dict:
    """Apply every fixed_code entry. Multiple entries on the same file are
    applied sequentially against the evolving file content (the second
    entry sees the first entry's edits)."""
    if not entries:
        return {"layer": None, "ok": False, "reason": "no fixed_code entries"}

    applied = []
    failed = []

    for entry in entries:
        rel = entry.get("file")
        if not rel:
            failed.append({"entry": entry, "reason": "missing file field"})
            continue
        target = flaky_root / rel
        if not target.exists():
            failed.append({"entry": entry, "reason": f"file not found: {target}"})
            continue
        try:
            src = target.read_text(encoding="utf-8")
            new_src, info = apply_fixed_code_entry(src, entry)
            target.write_text(new_src, encoding="utf-8")
            info["abs_path"] = str(target)
            applied.append(info)
        except Exception as e:
            failed.append({
                "entry": {k: v for k, v in entry.items() if k != "code"},
                "reason": str(e),
            })

    return {
        "layer": "splice output_b",
        "ok": len(failed) == 0 and len(applied) > 0,
        "applied": applied,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Compile verification (smoke test, not full build)
# ---------------------------------------------------------------------------

def _which(cmd: str):
    r = subprocess.run(["which", cmd], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _build_classpath(flaky_root: Path, m2_root: Path) -> str:
    parts = []
    for p in flaky_root.rglob("target/classes"):
        parts.append(str(p))
    for p in flaky_root.rglob("target/test-classes"):
        parts.append(str(p))
    if m2_root and m2_root.exists():
        for jar in m2_root.rglob("*.jar"):
            parts.append(str(jar))
    return ":".join(parts)


def verify_compile(flaky_root: Path, m2_root, touched_files: list) -> dict:
    """Compile each touched .java file with -d to a throwaway directory.
    This is a syntax/import smoke test, not a full build — we don't run tests."""
    javac = _which("javac")
    if javac is None:
        return {"skipped": True, "reason": "javac not on PATH"}

    java_files = [Path(f) for f in touched_files
                  if str(f).endswith(".java") and Path(f).exists()]
    if not java_files:
        return {"skipped": True, "reason": "no .java files touched"}

    cp = _build_classpath(flaky_root, m2_root) if m2_root else ""
    out_dir = "/tmp/applier_javac_out"
    os.makedirs(out_dir, exist_ok=True)

    results = []
    for jf in java_files:
        cmd = [javac, "-d", out_dir]
        if cp:
            cmd += ["-cp", cp]
        cmd.append(str(jf))
        r = subprocess.run(cmd, capture_output=True, text=True)
        rel = str(jf.relative_to(flaky_root)) if jf.is_relative_to(flaky_root) else str(jf)
        # Filter benign "annotation processor RELEASE_6" warnings — they're
        # noise from older deps and don't indicate a real problem.
        stderr = r.stderr or ""
        meaningful = "\n".join(
            ln for ln in stderr.splitlines()
            if "RELEASE_6" not in ln
            and "annotation processor" not in ln
            and "Annotation processing" not in ln
            and "-Xlint:-options" not in ln
            and "-proc:" not in ln
            and "(--processor-path" not in ln
            and not ln.strip().startswith("Use ")
        )
        results.append({
            "file": rel,
            "ok": r.returncode == 0,
            "stderr": meaningful.strip()[:2000],
        })

    return {
        "skipped": False,
        "all_ok": all(r["ok"] for r in results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Container recompile (regenerates target/test-classes)
# ---------------------------------------------------------------------------

# `mvn surefire:test` does NOT trigger compile/test-compile phases. After
# patching a test file in Flaky/, target/test-classes/ holds stale bytecode
# until something invokes test-compile explicitly. Surefire happily runs the
# old class — making every "did the LLM fix work?" check a false negative.
# We rebuild test-classes inside the container so the source/bytecode are
# in sync before any verification run.

def _module_for_file(flaky_root: Path, file_path: Path) -> str:
    """Walk up from `file_path` to the nearest pom.xml. Returns the module's
    relative path from `flaky_root`, or '.' for the root module.
    """
    flaky_resolved = flaky_root.resolve()
    p = file_path.resolve()
    if p.is_file():
        p = p.parent
    while True:
        if (p / "pom.xml").exists():
            try:
                return str(p.relative_to(flaky_resolved))
            except ValueError:
                return "."
        if p == flaky_resolved or p.parent == p:
            return "."
        p = p.parent


def _modules_to_recompile(flaky_root: Path, touched_files: list) -> list:
    """Collect unique Maven modules covering the touched files. If anything
    landed in the root module, return ['.'] so we recompile the whole tree.
    """
    modules = []
    seen = set()
    for f in touched_files:
        m = _module_for_file(flaky_root, f)
        if m not in seen:
            seen.add(m)
            modules.append(m)
    if "." in modules:
        return ["."]
    return modules


def _container_running(container: str) -> tuple:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return (False, f"container {container!r} not found")
    if r.stdout.strip() != "true":
        return (False, f"container {container!r} not running")
    return (True, None)


def recompile_in_container(container: str, modules: list) -> dict:
    """Run `mvn test-compile` inside the docker container for the touched
    modules. Mirrors the manual command we verified for dubbo:

        cd /app/work/Flaky && \\
        export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT && \\
        mvn test-compile -pl <modules> -am \\
            -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip \\
            -Denforcer.skip -Dmaven.javadoc.skip
    """
    if not modules:
        return {"skipped": True, "reason": "no modules to recompile"}

    if _which("docker") is None:
        return {"skipped": True, "reason": "docker not on PATH"}

    running, err = _container_running(container)
    if not running:
        return {"skipped": True, "reason": err}

    skip_flags = (
        "-Dgpg.skip=true -Dcheckstyle.skip -Drat.skip "
        "-Denforcer.skip -Dmaven.javadoc.skip"
    )
    if modules == ["."]:
        mvn_cmd = f"mvn test-compile {skip_flags}"
    else:
        pl_arg = ",".join(modules)
        mvn_cmd = f"mvn test-compile -pl {pl_arg} -am {skip_flags}"

    bash_cmd = (
        "cd /app/work/Flaky && "
        "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT && "
        + mvn_cmd
    )

    r = subprocess.run(
        ["docker", "exec", container, "bash", "-lc", bash_cmd],
        capture_output=True, text=True,
    )
    return {
        "skipped": False,
        "ok": r.returncode == 0,
        "container": container,
        "modules": modules,
        "command": bash_cmd,
        "stderr_tail": (r.stderr or "")[-2000:],
        "stdout_tail": (r.stdout or "")[-2000:],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _touched_files_from_patch(flaky_root: Path, patch_text: str) -> list:
    files = []
    if not patch_text:
        return files
    for m in re.finditer(r'^\+\+\+\s+b/(.+)$', patch_text, re.M):
        files.append(flaky_root / m.group(1).strip())
    return files


def _touched_files_from_fixed_code(flaky_root: Path, entries: list) -> list:
    files = []
    for e in entries or []:
        rel = e.get("file")
        if rel:
            files.append(flaky_root / rel)
    return files


def _save_report(base: Path, report: dict):
    out = base / "Steps Output Files" / "apply_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Report saved: {out}")


def _print_summary(report: dict):
    """Binary [PASS]/[FAIL] reporting.

    The bytecode-validity verdict is driven by container `mvn test-compile`
    when available — it uses Maven's authoritative classpath construction
    and matches what downstream surefire actually reads. Host-side `javac`
    is brittle on real Maven projects (Lombok ↔ JDK module-system mismatch,
    classpath heuristics) and produces frequent false negatives even when
    the project compiles cleanly via Maven. So we print host-compile
    informationally with a `[INFO]` label when the container recompile
    already gave us the authoritative answer, and only let host-compile
    drive the verdict when the container recompile didn't run."""
    print()
    print("=" * 60)
    print(f"APPLY REPORT  container={report['container']}")
    print("=" * 60)
    for layer in report["layers_attempted"]:
        verdict = "[PASS]" if layer.get("ok") else "[FAIL]"
        name = layer.get("layer") or "(none)"
        detail = layer.get("reason") or ""
        if "applied" in layer:
            detail = f"{len(layer['applied'])} applied, {len(layer['failed'])} failed"
        print(f"  {verdict} {name:30s} {detail}")
        if layer.get("failed"):
            for f in layer["failed"]:
                print(f"           - {f['reason']}")

    rc = report.get("recompile") or {}
    recompile_ran = bool(rc) and not rc.get("skipped")
    recompile_ok = recompile_ran and bool(rc.get("ok"))

    if report.get("compile"):
        c = report["compile"]
        # Label: PASS/FAIL only when host compile is the verdict driver.
        # When the container recompile has already given us the authoritative
        # answer, host-compile is INFO — its result does not affect verdict.
        if c.get("skipped"):
            label = "[INFO]" if recompile_ran else "[FAIL]"
            print(f"  {label} compile (host javac): skipped ({c.get('reason')})")
        else:
            host_ok = c.get("all_ok")
            if recompile_ran:
                label = "[INFO]" if host_ok else "[INFO]"  # always INFO when authoritative recompile ran
            else:
                label = "[PASS]" if host_ok else "[FAIL]"
            n_ok = sum(1 for r in c["results"] if r["ok"])
            n_total = len(c["results"])
            suffix = "" if not recompile_ran else "  (informational; container recompile is authoritative)"
            print(f"  {label} compile (host javac): {n_ok}/{n_total} files OK{suffix}")
            for r in c["results"]:
                if not r["ok"]:
                    snippet = r["stderr"].splitlines()[0] if r["stderr"] else ""
                    print(f"           - {r['file']}: {snippet}")

    if report.get("recompile"):
        if rc.get("skipped"):
            print(f"  [FAIL] recompile: skipped ({rc.get('reason')})")
        else:
            verdict = "[PASS]" if rc.get("ok") else "[FAIL]"
            mods = ",".join(rc.get("modules", [])) or "(none)"
            print(f"  {verdict} recompile: mvn test-compile -pl {mods}")
            if not rc.get("ok"):
                tail = (rc.get("stderr_tail") or rc.get("stdout_tail") or "")
                for ln in tail.splitlines()[-5:]:
                    print(f"           {ln}")

    # Verdict: patch must have landed AND the patched bytecode must compile.
    # Container recompile is the authoritative compile signal; if it didn't
    # run, fall back to host-compile.
    landed_ok = bool(report.get("result", {}).get("ok"))
    if recompile_ran:
        bytecode_ok = recompile_ok
    else:
        c = report.get("compile") or {}
        bytecode_ok = bool(c) and (not c.get("skipped")) and bool(c.get("all_ok"))
    overall_ok = landed_ok and bytecode_ok

    final = report.get("result", {})
    print()
    if overall_ok:
        print(f"RESULT: PASS — applied via {final.get('layer')}, compiles cleanly")
    elif landed_ok:
        if recompile_ran:
            print(f"RESULT: FAIL — patch landed via {final.get('layer')}, but mvn test-compile failed (see above)")
        else:
            print(f"RESULT: FAIL — patch landed via {final.get('layer')}, but compile could not be confirmed (see above)")
    else:
        print(f"RESULT: FAIL — {final.get('reason', 'no layer landed')}")


def main():
    parser = argparse.ArgumentParser(description="Apply LLM fix to Flaky/ tree")
    parser.add_argument("container", help="result_container directory name")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip javac compile verification")
    parser.add_argument("--no-recompile", action="store_true",
                        help="skip container-side mvn test-compile")
    parser.add_argument("--docker-container",
                        help="docker container name (default: tm_<container>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="check only; don't modify files")
    args = parser.parse_args()

    base = DATA_DIR / args.container
    if not base.is_dir():
        sys.exit(f"ERROR: {base} not found")

    flaky = base / "Flaky"
    if not flaky.is_dir():
        sys.exit(f"ERROR: {flaky} not found")

    response_path = base / "Steps Output Files" / "llm_response.json"
    if not response_path.is_file():
        sys.exit(f"ERROR: {response_path} not found")

    response = json.loads(response_path.read_text(encoding="utf-8"))
    output_a_patch = (response.get("response", {})
                              .get("output_a", {})
                              .get("patch"))
    fixed_code = (response.get("response", {})
                          .get("output_b", {})
                          .get("fixed_code", []))

    report = {
        "container": args.container,
        "flaky_root": str(flaky),
        "dry_run": args.dry_run,
        "layers_attempted": [],
        "result": None,
    }

    landed = None

    # ---- Layer 1 & 2: output_a patch ----
    if output_a_patch:
        if args.dry_run:
            r = check_patch(flaky, output_a_patch)
            r["layer"] = (r.get("layer") or "output_a") + " (dry-run)"
            report["layers_attempted"].append(r)
            if r.get("ok"):
                landed = r
        else:
            r = apply_patch(flaky, output_a_patch)
            report["layers_attempted"].append(r)
            if r.get("ok"):
                landed = r
    else:
        report["layers_attempted"].append(
            {"layer": "output_a", "ok": False, "reason": "no patch in response"})

    # ---- Layer 3: splice output_b.fixed_code ----
    if landed is None and fixed_code:
        if args.dry_run:
            report["layers_attempted"].append({
                "layer": "splice output_b (dry-run)",
                "ok": True,
                "reason": f"would splice {len(fixed_code)} entr"
                          f"{'y' if len(fixed_code) == 1 else 'ies'}",
            })
        else:
            r = apply_fixed_code(flaky, fixed_code)
            report["layers_attempted"].append(r)
            if r.get("ok"):
                landed = r

    # ---- Compile verification + container recompile ----
    if landed and not args.dry_run:
        touched = _touched_files_from_patch(flaky, output_a_patch) + \
                  _touched_files_from_fixed_code(flaky, fixed_code)
        # Dedup while preserving order
        seen = set()
        deduped = []
        for f in touched:
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                deduped.append(f)

        if not args.no_verify:
            m2 = base / "Flakym2" / ".m2" / "repository"
            report["compile"] = verify_compile(flaky, m2 if m2.exists() else None, deduped)

        if not args.no_recompile:
            # Match the sanitization done by run_{td,od}_tracemop.sh
            #   CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"
            # so containers like "BOOKKEEPER-846" -> "tm_BOOKKEEPER_846"
            # are findable when apply_fix.py is invoked standalone.
            sanitized = re.sub(r'[^a-zA-Z0-9]', '_', args.container)
            container_name = args.docker_container or f"tm_{sanitized}"
            modules = _modules_to_recompile(flaky, deduped)
            report["recompile"] = recompile_in_container(container_name, modules)

    report["result"] = landed or {
        "layer": None,
        "ok": False,
        "reason": "no layer landed the fix",
    }

    _save_report(base, report)
    _print_summary(report)

    # Exit 0 only if everything PASSED end-to-end. Container `mvn test-compile`
    # is the authoritative bytecode-validity signal (it uses Maven's real
    # classpath construction); host `javac` is informational only, because it
    # produces false negatives on Lombok / JDK-module-system / multi-module
    # projects even when the project compiles fine via Maven.
    #
    # Rule: PASS iff (a) a layer landed AND (b) container recompile passed.
    # Fallback when recompile didn't run: host compile must have run and passed.
    landed_ok = bool(report["result"].get("ok"))
    rc = report.get("recompile") or {}
    if rc and not rc.get("skipped"):
        bytecode_ok = bool(rc.get("ok"))
    else:
        c = report.get("compile") or {}
        bytecode_ok = bool(c) and (not c.get("skipped")) and bool(c.get("all_ok"))

    sys.exit(0 if (landed_ok and bytecode_ok) else 1)


if __name__ == "__main__":
    main()

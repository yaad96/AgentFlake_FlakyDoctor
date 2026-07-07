#!/usr/bin/env python3
"""
patch_to_diff.py - turn a FlakyDoctor run's LLM fixes into a readable,
whitespace-insensitive semantic diff (original method -> the LLM's suggested
method), for EVERY repair round - both the passing fix AND the failing attempts.

Indentation-only differences are ignored (the model often re-indents the whole
method), so only real code changes show as +/-; any imports / pom dependency the
model added are appended as explicit + sections.

Source of truth is a run's `details.json`, which records every round in
`patches_before_stitching` (pass or fail) alongside the original method.

Usage (from the FlakyDoctor root):
  # a run dir (or its details.json) -> writes <dir>/semantic_diff.diff
  python3 runner/patch_to_diff.py outputs/af_fd_<...>
  python3 runner/patch_to_diff.py data/<container>/run_03/flakydoctor_output
  python3 runner/patch_to_diff.py path/to/details.json --out semantic_diff.diff
  python3 runner/patch_to_diff.py <dir> --stdout          # print instead of writing

  # a single FlakyDoctor GoodPatches .patch file -> diff on stdout
  python3 runner/patch_to_diff.py path/to/1.patch
"""
import sys, os, re, json, difflib, argparse

# ---------------------------------------------------------------------------
# whitespace-insensitive semantic diff engine
# ---------------------------------------------------------------------------
def _trim(s):
    lines = (s or "").split("\n")
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return lines


def semantic_diff(orig, fix, path, label):
    """Lines equal after .strip() are context; only real content changes show as
    -/+ (removed keeps the original indent, context/added keep the model's)."""
    o, f = _trim(orig), _trim(fix)
    so, sf = [l.strip() for l in o], [l.strip() for l in f]
    if so == sf:
        return f"# {label}: unchanged (only formatting differs)\n"
    sm = difflib.SequenceMatcher(None, so, sf, autojunk=False)
    out = [f"--- a/{path}  ({label}, original)", f"+++ b/{path}  ({label}, LLM suggestion)"]
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += [" " + l for l in f[j1:j2]]
        else:
            out += ["-" + l for l in o[i1:i2]]
            out += ["+" + l for l in f[j1:j2]]
    return "\n".join(out) + "\n"


def parse_imports(s):
    s = (s or "").strip()
    if s in ("", "[]", "[ ]", "None"):
        return []
    try:
        import ast
        v = ast.literal_eval(s)
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        pass
    return re.findall(r'import\s+(?:static\s+)?[^\n;]+;', s)


def parse_pom(s):
    s = (s or "").strip()
    s = re.sub(r'^```[a-zA-Z]*', '', s)
    s = re.sub(r'```$', '', s).strip()
    if s in ("", "None") or "no new dep" in s.lower():
        return ""
    if s.startswith("<!--") and "<dependency" not in s:
        return ""
    return s


def extras(imports, pom_raw):
    """Render the imports Claude added and any pom.xml dependency as + sections.
    `imports` may be a list (from details.json) or a raw string (from a .patch)."""
    imps = parse_imports(imports) if isinstance(imports, str) else \
        [str(i).strip() for i in (imports or []) if str(i).strip()]
    pom = parse_pom(pom_raw)
    out = ""
    if imps:
        out += "\n# ===== Imports added by Claude =====\n" + "\n".join("+" + i for i in imps) + "\n"
    if pom:
        out += "\n# ===== Maven dependency added by Claude (pom.xml) =====\n" + \
               "\n".join("+" + l for l in pom.split("\n")) + "\n"
    return out


# ---------------------------------------------------------------------------
# details.json -> full semantic diff of every round (passing + failing)
# ---------------------------------------------------------------------------
def _iter_objs(details_path):
    """details.json is a stream of concatenated JSON objects (one per test)."""
    raw = open(details_path, encoding="utf-8", errors="replace").read().strip()
    dec, i = json.JSONDecoder(), 0
    while i < len(raw):
        obj, end = dec.raw_decode(raw, i)
        yield obj
        i = end
        while i < len(raw) and raw[i] in " \n\t,":
            i += 1


def details_to_diff(details_path):
    out = [f"# semantic diff of every LLM repair round (passing + failing)\n",
           f"# source: {details_path}\n"]
    for obj in _iter_objs(details_path):
        is_od = "victim_method_content" in obj
        name = obj.get("victim") or obj.get("test") or "?"
        results = obj.get("test_results", {}) or {}
        patches = obj.get("patches_before_stitching", {}) or {}
        path = (obj.get("relative_victim_file_path") or obj.get("victim_file_path")
                or obj.get("file_path") or name)

        if not patches:
            out.append(f"\n{'=' * 72}\n{name}: no LLM suggestion (run produced no patch)\n{'=' * 72}\n")
            if obj.get("Exceptions"):
                out.append(f"# Exceptions: {obj['Exceptions']}\n")
            continue

        for r in sorted(patches, key=lambda x: int(x)):
            p = patches[r] or {}
            verdict = results.get(r) or results.get(str(r)) or "no result"
            tag = "   <-- PASSED" if verdict == "test_pass" else "   (failing attempt)"
            out.append(f"\n{'=' * 72}\n{name} - round {r} ({verdict}){tag}\n{'=' * 72}\n")
            if is_od:
                out.append("# ----- VICTIM method -----\n" +
                           semantic_diff(obj.get("victim_method_content"),
                                         p.get("victim_test_code"), path, "victim"))
                out.append("# ----- POLLUTER method -----\n" +
                           semantic_diff(obj.get("polluter_method_content"),
                                         p.get("polluter_test_code"), path, "polluter"))
            else:
                out.append(semantic_diff(obj.get("test_method_content"),
                                         p.get("test_code"), path, "test method"))
            out.append(extras(p.get("import"), str(p.get("pom") or "")))
    return "".join(out)


# ---------------------------------------------------------------------------
# single GoodPatches .patch file -> diff (backward compatible)
# ---------------------------------------------------------------------------
MARKERS = ["Test File Path:", "Original Polluter Method:", "Original Victim Method:",
           "Original Test Method:", "Patch:", "test_code:", "victim_test_code:",
           "polluter_test_code:", "import:", "pom:"]


def split_blocks(text):
    pos = []
    for m in MARKERS:
        mt = re.search(r'(?m)^[ \t]*' + re.escape(m), text)
        if mt:
            pos.append((m, mt.start(), mt.end()))
    pos.sort(key=lambda x: x[1])
    b = {}
    for i, (m, s, e) in enumerate(pos):
        b[m] = text[e:(pos[i + 1][1] if i + 1 < len(pos) else len(text))]
    return b


def patch_to_diff(text):
    b = split_blocks(text)
    path = " ".join(b.get("Test File Path:", "").split())
    if "Original Test Method:" in b:                       # ---- ID ----
        content = semantic_diff(b.get("Original Test Method:", ""), b.get("test_code:", ""),
                                path, "test method")
    else:                                                  # ---- OD ----
        content = ("# ===== VICTIM method =====\n" +
                   semantic_diff(b.get("Original Victim Method:", ""), b.get("victim_test_code:", ""),
                                 path, "victim") +
                   "\n# ===== POLLUTER method =====\n" +
                   semantic_diff(b.get("Original Polluter Method:", ""), b.get("polluter_test_code:", ""),
                                 path, "polluter"))
    return content + extras(b.get("import:", ""), b.get("pom:", ""))


# ---------------------------------------------------------------------------
def resolve_details(path):
    """Accept a details.json, a run dir, or a flakydoctor_output dir."""
    if os.path.isfile(path):
        return path
    for cand in (os.path.join(path, "details.json"),
                 os.path.join(path, "flakydoctor_output", "details.json")):
        if os.path.isfile(cand):
            return cand
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a run dir / details.json (-> semantic_diff.diff), "
                                  "or a single .patch file (-> stdout)")
    ap.add_argument("--out", help="output path (default: semantic_diff.diff next to details.json)")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing a file")
    a = ap.parse_args()

    # single GoodPatches .patch file -> stdout
    if a.path.endswith(".patch") and os.path.isfile(a.path):
        sys.stdout.write(patch_to_diff(open(a.path, encoding="utf-8", errors="replace").read()))
        return

    details = resolve_details(a.path)
    if not details:
        ap.error(f"no details.json found at or under {a.path}")
    text = details_to_diff(details)
    if a.stdout:
        sys.stdout.write(text)
    else:
        out = a.out or os.path.join(os.path.dirname(details), "semantic_diff.diff")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()

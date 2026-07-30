#!/usr/bin/env python3
"""Filter a FlakyCodeChange forcing diff down to its applicable source hunks.

The dataset's FlakyCodeChange.patch is produced with `diff -ruN Flaky FlakyCodeChange`
over two *built* trees, so it carries stale build artifacts: "Binary files ... differ"
entries (.jar/.class) and text hunks under target/ (generated META-INF/NOTICE, etc.).
`patch` cannot apply binary diffs, and the target/ files don't match a freshly-staged
tree, so with fuzz 0 (-F0) a single failed hunk aborts the whole apply -- which the TD
driver then misreports as "is `patch` installed?". The only meaningful part of a timing
forcing is the source change (e.g. an injected Thread.sleep). This keeps exactly those
sections: it drops standalone binary entries and any file section whose path is under a
build-output dir (target/, build/, out/, bin/). Reads the raw diff on stdin, writes the
filtered diff on stdout. If nothing survives, output is empty (caller decides what to do).
"""
import sys

BUILD_DIRS = ("/target/", "/build/", "/out/", "/bin/")


def _under_build_output(path: str) -> bool:
    p = "/" + path.lstrip("/")
    return any(seg in p for seg in BUILD_DIRS) or path.startswith(
        ("target/", "build/", "out/", "bin/")
    )


def main() -> int:
    lines = sys.stdin.read().split("\n")
    out = []
    keep = False  # whether the current file section should be emitted
    for line in lines:
        if line.startswith("diff "):
            # text-file section header: `diff -ruN <old> <new>`; decide by the new path
            parts = line.split()
            new_path = parts[-1] if len(parts) >= 2 else ""
            keep = not _under_build_output(new_path)
            if keep:
                out.append(line)
            continue
        if line.startswith("Binary files ") and line.rstrip().endswith("differ"):
            # standalone binary entry -- never appliable; also closes any open section
            keep = False
            continue
        if keep:
            out.append(line)
    sys.stdout.write("\n".join(out))
    if out and not out[-1].endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

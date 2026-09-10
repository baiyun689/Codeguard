"""Offline integrity check for a versioned local suite (no model calls)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def verify(root: Path) -> dict:
    frozen = json.loads((root / "freeze.json").read_text(encoding="utf-8"))
    for case_id, expected in frozen["cases"].items():
        folder = root / "cases" / case_id
        if folder.resolve().parent != (root / "cases").resolve():
            raise ValueError("Case path escapes suite")
        for name in ("case.yaml", "changes.diff"):
            actual = hashlib.sha256((folder / name).read_bytes()).hexdigest()
            if actual != expected[name]:
                raise ValueError(f"Frozen input changed: {case_id}/{name}")
        tree = subprocess.check_output(
            ["git", "-C", str(folder / "repo"), "rev-parse", "HEAD^{tree}"], text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(folder / "repo"), "status", "--porcelain", "--untracked-files=all"], text=True
        ).strip()
        if tree != expected["baseline_tree"] or status:
            raise ValueError(f"Baseline changed: {case_id}")
    from evals.dataset import load_cases

    cases = load_cases(root)
    if {case.id for case in cases} != set(frozen["cases"]):
        raise ValueError("Manifest and frozen cases differ")
    return {"cases": len(cases), "registered_defects": sum(len(c.expected) for c in cases),
            "clean_cases": sum(not c.expected for c in cases), "integrity": "passed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.dataset), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

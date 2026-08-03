"""planted-bugs 级命中分析(人工计指标辅助,非 runner 链路)。

从 checkpoint 提取 reported_issues,与 `_bugs_gt.json`(由 planted-bugs.diff 按 hunk 聚合)
按 file + 行号 ±10 硬命中 / 描述 token 重叠(≥2)软命中匹配,聚合出 bug 级
Recall(任一 2 轮命中)与 Precision(严格/宽松)。

用法:
    python -m evals.recall_analyzer [--retry-db checkpoint-direct-retry.db]
```

口径说明(与 01-初步指标.md 一致):
- bug 级 Recall = 该 case 的 bug 中,2 轮任一命中 / 总 bug 数
- Precision 严格 = 每 bug 首个命中 issue 算 TP,其余命中 issue 算 FP
- Precision 宽松 = 所有命中 issue 都算 TP
- 空输出轮按 0 命中计入(异常轮次在报告中标注)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_CASES_DIR = Path(__file__).resolve().parent / "dataset" / "selected-20-v2"
_GT_FILE = _CASES_DIR / "cases" / "_bugs_gt.json"
_PROFILES = {
    "direct": [
        "checkpoint-direct.db",
        "checkpoint-direct-b2.db",
        "checkpoint-direct-b3.db",
        "checkpoint-direct-b4.db",
    ],
    "full": [
        "checkpoint-full.db",
        "checkpoint-full-b2.db",
        "checkpoint-full-b3.db",
        "checkpoint-full-b4.db",
    ],
}
# 补跑 case:retry checkpoint 的轮次优先于旧 checkpoint(旧轮空输出)
_RETRY_CASES = {
    "gitbug-cloudsimplus-argument-order",
    "gitbug-quality-cbor-type",
    "gitbug-s3-root-directories",
    "gitbug-dos-nbvcxz",
    "gitbug-epub-referenced-items",
    "gitbug-robots-unicode",
}


def _norm(s: object) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def match(issue: dict, bug: dict) -> bool:
    """file 相同 + 行号 ±10 硬命中;偏移大时用描述 token 重叠(≥2)软命中。"""
    if _norm(issue.get("file")) != _norm(bug["file"]):
        return False
    il, bl = issue.get("line") or 0, bug["line"]
    if abs(il - bl) <= 10:
        return True
    it = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", _norm(issue.get("message", "")) + _norm(issue.get("summary", ""))))
    bt = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", _norm(bug["desc"])))
    return len(it & bt) >= 2


def load_runs(profile: str, retry_db: Path | None) -> dict[str, list[tuple[int, list[dict]]]]:
    """{case_id: [(round_idx, issues), ...]};retry checkpoint 覆盖同 case 的旧轮次。"""
    files = list(_PROFILES[profile])
    if retry_db is not None and profile == "direct":
        # 补跑仅针对 direct 空输出轮;full 保持原 checkpoint
        files.insert(0, retry_db.name)
    out: dict[str, list[tuple[int, list[dict]]]] = {}
    for fname in files:
        db = _CASES_DIR / fname
        if not db.exists():
            continue
        d = json.loads(db.read_text(encoding="utf-8"))
        for case_runs in d.get("runs", []):
            for cr in case_runs:
                cid = cr["case_id"]
                if profile == "direct" and retry_db is not None and cid in _RETRY_CASES and db != retry_db:
                    continue  # 该 case 已由 retry 轮次覆盖,忽略旧轮
                out.setdefault(cid, []).append((len(out.get(cid, [])), cr.get("reported_issues") or []))
    return out


def analyze(profile: str, bugs: dict, retry_db: Path | None) -> dict:
    runs = load_runs(profile, retry_db)
    per_case, empty = [], []
    for cid in sorted(runs):
        cb = bugs.get(cid, [])
        issues_all: list[dict] = []
        hit_bugs: set[int] = set()
        n_empty = 0
        for _, issues in runs[cid]:
            issues_all.extend(issues)
            if not issues:
                n_empty += 1
        for b_idx, b in enumerate(cb):
            if any(match(iss, b) for iss in issues_all):
                hit_bugs.add(b_idx)
        per_case.append({"case": cid, "total": len(cb), "hit": len(hit_bugs), "reported": len(issues_all), "empty_rounds": n_empty})
        if n_empty:
            empty.append(cid)
    total = sum(r["total"] for r in per_case)
    hit = sum(r["hit"] for r in per_case)
    reported = sum(r["reported"] for r in per_case)
    # 逐 issue:宽松 TP = 匹配任一 bug 的 issue 数;严格 TP = 每 bug 首个命中 issue 数(重复报告算 FP)
    tp_loose = 0
    first_hit: set[tuple[str, int]] = set()
    for cid, issue_list in runs.items():
        cb = bugs.get(cid, [])
        for _, issues in issue_list:
            for iss in issues:
                if any(match(iss, b) for b in cb):
                    tp_loose += 1
        for b_idx, b in enumerate(cb):
            for _, issues in issue_list:
                if any(match(iss, b) for iss in issues):
                    first_hit.add((cid, b_idx))
                    break
    return {
        "profile": profile,
        "total_bugs": total,
        "hit_bugs": hit,
        "recall": hit / total if total else 0.0,
        "reported_issues": reported,
        "tp_strict": len(first_hit),
        "precision_strict": len(first_hit) / reported if reported else 0.0,
        "precision_loose": tp_loose / reported if reported else 0.0,
        "per_case": per_case,
        "empty_cases": empty,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="planted-bugs 级命中分析")
    parser.add_argument("--retry-db", default="", help="补跑 checkpoint 文件名(覆盖同 case 旧轮)")
    args = parser.parse_args()
    retry_db = Path(args.retry_db) if args.retry_db else None
    if retry_db is not None:
        retry_db = _CASES_DIR / retry_db.name
    bugs = json.loads(_GT_FILE.read_text(encoding="utf-8"))
    results = [analyze(p, bugs, retry_db) for p in ("direct", "full")]
    print(f"{'profile':8s} {'Recall':>8s} {'命中':>7s} {'报告':>6s} {'P严格':>7s} {'P宽松':>7s} {'空轮case':>10s}")
    for r in results:
        print(
            f"{r['profile']:8s} {r['recall']:8.3f} {r['hit_bugs']:4d}/{r['total_bugs']:<3d} "
            f"{r['reported_issues']:6d} {r['precision_strict']:7.3f} {r['precision_loose']:7.3f} "
            f"{len(r['empty_cases']):3d} {','.join(r['empty_cases'])[:40]}"
        )
    print("\n每 case 命中:")
    for r in results:
        print(f"--- {r['profile']} ---")
        for c in r["per_case"]:
            empty = f" (空{''.join('•' for _ in range(c['empty_rounds']))})" if c["empty_rounds"] else ""
            print(f"  {c['case']:35s} {c['hit']:2d}/{c['total']}  报告 {c['reported']:3d}{empty}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

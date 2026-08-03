"""跨文件/高危缺陷归因分析(简历数据支撑,非 runner 链路)。

对 `_bugs_gt.json` 的每个 bug,给 LLM 看该 bug 所在 hunk 的 diff 上下文
+ 漏洞行/修复行对照,判定:
    - cross_file:仅凭该 diff(单文件变更)能否发现此缺陷?根因是否依赖 diff 之外的文件?
与 checkpoint 命中匹配(复用 recall_analyzer 口径),汇总高危(Vul4J 真实 CVE)子集的检出对比。

用法:
    python -m evals.cross_file_analyzer            # 用缓存归因结果出统计
    python -m evals.cross_file_analyzer --rerun    # 忽略缓存重新归因(调 LLM)
    python -m evals.cross_file_analyzer --limit 5  # 只归因前 5 个(调试)

归因结果缓存:`selected-20-v2/cases/_cross_file_gt.json`(与 _bugs_gt.json 并排,不随仓库分发)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_CASES_DIR = Path(__file__).resolve().parent / "dataset" / "selected-20-v2"
_GT_FILE = _CASES_DIR / "cases" / "_bugs_gt.json"
_CROSS_FILE_FILE = _CASES_DIR / "cases" / "_cross_file_gt.json"

# 高危 = Vul4J 真实 CVE 安全漏洞(GitBug 为功能性缺陷,不算高危)。
_HIGH_RISK_PREFIX = "vul4j"


def _load_gt() -> dict:
    return json.loads(_GT_FILE.read_text(encoding="utf-8"))


def parse_hunks(diff_text: str) -> list[dict]:
    """解析 planted-bugs.diff → [{file, hunks:[{old_start, new_start, lines:{num: text}}]}]。

    "-" 删除行保留(行号按 old 段),"+"/上下文行按 new 段——审查员看 diff 时两类都可见,
    删除的防御分支往往正是缺陷所在,不能丢。
    """
    files: list[dict] = []
    cur: dict | None = None
    for raw in diff_text.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/", raw)
        if m:
            cur = {"file": m.group(1), "hunks": []}
            files.append(cur)
            continue
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m and cur is not None:
            cur["hunks"].append(
                {"old_start": int(m.group(1)), "new_start": int(m.group(2)), "lines": {}}
            )
            continue
        if cur is not None and cur["hunks"] and raw[:1] in "+- ":
            h = cur["hunks"][-1]
            if raw.startswith("-"):
                num = h["old_start"] + len(
                    [v for v in h["lines"].values() if v.startswith("-")]
                )
            else:
                num = h["new_start"] + len(
                    [v for v in h["lines"].values() if not v.startswith("-")]
                )
            h["lines"][num] = raw
    return files


def find_bug_hunk(diff_text: str, file: str, line: int) -> tuple[str | None, int | None]:
    """返回 (hunk 上下文文本, 该行是否在 hunk 变更行内)。文件不匹配返回 (None, None)。"""
    for f in parse_hunks(diff_text):
        if f["file"] != file:
            continue
        for h in f["hunks"]:
            nums = sorted(h["lines"])
            if not nums:
                continue
            lo, hi = nums[0], nums[-1]
            if lo - 3 <= line <= hi + 3:
                ctx = "\n".join(f"{n:6d} {t}" for n, t in h["lines"].items())
                return ctx, line in h["lines"]
    return None, None


def bug_prompt(case_id: str, bug: dict, diff_text: str) -> str:
    ctx, _ = find_bug_hunk(diff_text, bug["file"], bug["line"])
    parts = bug["desc"].split("|", 1)
    bug_code = parts[0].strip()
    fix_code = parts[1].strip() if len(parts) > 1 else "(未提供修复对照)"
    return f"""你是代码审查数据集分析员。一个"植入缺陷"案例:

- 案例: {case_id}
- 缺陷所在文件: {bug['file']} (行号 {bug['line']})
- 该文件的变更 hunk(含上下文)——这是审查员拿到的全部材料:
```
{ctx or '(未找到对应 hunk)'}
```
- 缺陷行(漏洞版): {bug_code.strip()}
- 修复行(仅供你理解缺陷本质,**审查员看不到它**): {fix_code.strip()}

判定标准:一位资深审查员**只看上面的 diff**(没有修复行对照、看不到仓库其他文件),
是否会在该行**产生怀疑/发现异常**?
- diff 内可见(→ cross_file=false):缺陷迹象本身就在 diff 里——比如条件反转、参数顺序
  互换、常量阈值改坏、删掉了防御分支、明显的方法调用写错。审查员看到就能起疑。
- 跨文件(→ cross_file=true):diff 里的代码**每个局部都自洽**,只有对照 diff 之外的信息
  (调用方、相关方法定义、跨类状态、配置/框架入口)才能看出错了——比如两个方法体被互换、
  某方法被另一文件中的代码调用时才会触发异常。

拿不准时倾向 false:只有确凿需要外部上下文才能发现才判 true。

输出 JSON: {{"cross_file": true/false, "reason": "一句话理由"}}"""


def _llm():
    from langchain_openai import ChatOpenAI

    from codeguard_agent.config import Settings

    s = Settings.from_env()
    return ChatOpenAI(
        model=s.model,
        api_key=s.api_key,
        base_url=s.api_base_url,
        temperature=0,
    )


def attribute(case_id: str, bug: dict, diff_text: str) -> dict:
    """单 bug 归因,失败返回未知(不抛断)。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = _llm()
    for attempt in range(2):
        try:
            out = llm.invoke(
                [
                    SystemMessage(content="你只输出合法的 JSON,不要输出任何其他文字。"),
                    HumanMessage(content=bug_prompt(case_id, bug, diff_text)),
                ]
            )
            text = str(out.content)
            m = re.search(r"\{.*\}", text, re.DOTALL)
            payload = json.loads(m.group(0)) if m else {}
            return {
                "cross_file": bool(payload.get("cross_file")),
                "reason": str(payload.get("reason", ""))[:200],
            }
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                continue
            return {"cross_file": None, "reason": f"llm_error:{exc}"[:200]}


def run_attribution(limit: int | None = None) -> dict:
    """全量归因并落缓存。返回 {case_id: [{file, line, cross_file, reason}]}。"""
    gt = _load_gt()
    out: dict = {}
    done = 0
    for case_id, bugs in sorted(gt.items()):
        diff_text = (_CASES_DIR / "cases" / case_id / "planted-bugs.diff").read_text(
            encoding="utf-8", errors="replace"
        )
        out[case_id] = []
        for bug in bugs:
            if limit is not None and done >= limit:
                return out
            done += 1
            verdict = attribute(case_id, bug, diff_text)
            out[case_id].append({**{k: bug[k] for k in ("file", "line")}, **verdict})
            print(f"[{done:3d}] {case_id} {bug['file']}:{bug['line']} cross_file={verdict['cross_file']}", flush=True)
    _CROSS_FILE_FILE.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


def analyze() -> int:
    gt = _load_gt()
    if _CROSS_FILE_FILE.exists():
        attribution = json.loads(_CROSS_FILE_FILE.read_text(encoding="utf-8"))
    else:
        print("无缓存归因结果,先跑 --rerun", file=sys.stderr)
        return 2

    # 命中:full 用原 checkpoint,direct 用 retry 覆盖(与定稿口径一致)
    from evals.recall_analyzer import load_runs, match

    full_runs = load_runs("full", None)
    direct_runs = load_runs("direct", _CASES_DIR / "checkpoint-direct-retry.db")
    full_hit: set[tuple[str, int]] = set()
    direct_hit: set[tuple[str, int]] = set()
    for cid, bugs in gt.items():
        full_issues = [iss for _, isslist in full_runs.get(cid, []) for iss in isslist]
        direct_issues = [iss for _, isslist in direct_runs.get(cid, []) for iss in isslist]
        for i, b in enumerate(bugs):
            if any(match(iss, b) for iss in full_issues):
                full_hit.add((cid, i))
            if any(match(iss, b) for iss in direct_issues):
                direct_hit.add((cid, i))

    rows = []
    for cid, bugs in sorted(gt.items()):
        attrs = {bug["line"]: bug for bug in attribution.get(cid, [])}
        for i, b in enumerate(bugs):
            a = attrs.get(b["line"], {})
            rows.append(
                {
                    "case": cid,
                    "high_risk": cid.startswith(_HIGH_RISK_PREFIX),
                    "cross_file": a.get("cross_file"),
                    "full": (cid, i) in full_hit,
                    "direct": (cid, i) in direct_hit,
                    "file": b["file"],
                    "line": b["line"],
                }
            )

    total = len(rows)
    known = [r for r in rows if r["cross_file"] is not None]
    print(f"共 {total} bugs,归因成功 {len(known)}")
    for label, sub in (
        ("全量", rows),
        ("高危(Vul4J)", [r for r in rows if r["high_risk"]]),
        ("非高危(GitBug)", [r for r in rows if not r["high_risk"]]),
    ):
        s = [r for r in sub if r["cross_file"] is not None]
        cf = [r for r in s if r["cross_file"]]
        fh = sum(1 for r in cf if r["full"])
        dh = sum(1 for r in cf if r["direct"])
        fh_all = sum(1 for r in s if r["full"])
        dh_all = sum(1 for r in s if r["direct"])
        print(f"\n== {label}: 归因 {len(s)}/{len(sub)} ==")
        print(f"  跨文件缺陷: {len(cf)} 个;full 检出 {fh}/{len(cf)} ({(fh/len(cf)*100) if cf else 0:.1f}%),direct 检出 {dh}/{len(cf)}")
        print(f"  (该子集整体: full {fh_all}/{len(s)},direct {dh_all}/{len(s)})")
        for r in cf:
            print(f"    {'√' if r['full'] else '×'}{'√' if r['direct'] else '×'}  {r['case']} {r['file']}:{r['line']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="跨文件/高危缺陷归因统计")
    parser.add_argument("--rerun", action="store_true", help="忽略缓存重新归因(调 LLM)")
    parser.add_argument("--limit", type=int, default=None, help="归因数量上限(调试)")
    args = parser.parse_args()
    if args.rerun:
        run_attribution(args.limit)
        if args.limit is not None:
            return 0
    return analyze()


if __name__ == "__main__":
    sys.exit(main())

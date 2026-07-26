"""盲化人工裁决：池化额外发现、隐藏被测 profile、形成可重放决定。"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
from collections import defaultdict
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import BaseModel, Field

from codeguard_agent.models.schemas import Issue

from evals.schema import EvalCase, MatchOutcome

AdjudicationLabel = Literal[
    "novel-valid",
    "invalid",
    "duplicate",
    "out-of-scope",
    "uncertain",
]


class FindingOccurrence(BaseModel):
    """一条盲审 claim 在某个被测 profile/轮次中的出现位置。"""

    profile: str
    run_index: int
    case_id: str
    report_index: int


class AdjudicationTask(BaseModel):
    """供盲审的唯一 claim；occurrences 仅供重评分，不能渲染给 reviewer。"""

    id: str
    case_id: str
    category: str
    diff: str
    issue: Issue
    source_context: str = ""
    occurrences: list[FindingOccurrence] = Field(default_factory=list)


class AdjudicationBundle(BaseModel):
    version: str = "1"
    tasks: list[AdjudicationTask] = Field(default_factory=list)


class AdjudicationDecision(BaseModel):
    task_id: str
    reviewer_id: str
    label: AdjudicationLabel
    duplicate_of: str = ""
    note: str = ""
    is_resolution: bool = False


class ResolvedDecision(BaseModel):
    task_id: str
    label: AdjudicationLabel
    reviewer_ids: list[str] = Field(default_factory=list)
    duplicate_of: str = ""
    note: str = ""


class FinalizedAdjudication(BaseModel):
    resolved: dict[str, ResolvedDecision] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


def save_bundle(bundle: AdjudicationBundle, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")


def load_bundle(path: Path) -> AdjudicationBundle:
    return AdjudicationBundle.model_validate_json(path.read_text(encoding="utf-8"))


def load_decisions(path: Path) -> list[AdjudicationDecision]:
    if not path.is_file():
        return []
    decisions: dict[tuple[str, str], AdjudicationDecision] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        decision = AdjudicationDecision.model_validate_json(line)
        decisions[(decision.task_id, decision.reviewer_id)] = decision
    return list(decisions.values())


def save_decision(path: Path, decision: AdjudicationDecision) -> None:
    """按(task, reviewer)覆盖旧选择并原子落盘，允许 reviewer 返回修改。"""
    decisions = {
        (item.task_id, item.reviewer_id): item
        for item in load_decisions(path)
    }
    decisions[(decision.task_id, decision.reviewer_id)] = decision
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    rows = [
        item.model_dump_json()
        for _, item in sorted(decisions.items())
    ]
    temporary.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    temporary.replace(path)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _finding_key(case_id: str, issue: Issue) -> str:
    """稳定池化键。

    行号按三行窗口归桶，吸收模型常见定位漂移；人工仍可把语义近似但未自动合并的
    task 标为 duplicate 并指向同一 canonical task。
    """
    path = issue.file.replace("\\", "/").strip().lower()
    line_bucket = max(0, int(issue.line)) // 3
    material = "\n".join(
        (case_id, path, str(line_bucket), _normalise(issue.type), _normalise(issue.message))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def build_blind_bundle(
    cases: list[EvalCase],
    profile_runs: dict[str, list[list[MatchOutcome]]],
) -> AdjudicationBundle:
    """汇总所有 profile/轮次的未匹配最终报告，并生成来源盲化任务。"""
    case_by_id = {case.id: case for case in cases}
    tasks: dict[str, AdjudicationTask] = {}
    for profile, runs in profile_runs.items():
        for run_index, outcomes in enumerate(runs):
            for outcome in outcomes:
                case = case_by_id.get(outcome.case_id)
                if case is None:
                    continue
                for report_index in outcome.unmatched_report_indices:
                    if not 0 <= report_index < len(outcome.reported_issues):
                        continue
                    issue = outcome.reported_issues[report_index]
                    task_id = _finding_key(case.id, issue)
                    task = tasks.setdefault(
                        task_id,
                        AdjudicationTask(
                            id=task_id,
                            case_id=case.id,
                            category=case.category,
                            diff=case.diff,
                            issue=issue,
                        ),
                    )
                    task.occurrences.append(
                        FindingOccurrence(
                            profile=profile,
                            run_index=run_index,
                            case_id=case.id,
                            report_index=report_index,
                        )
                    )
    return AdjudicationBundle(tasks=sorted(tasks.values(), key=lambda task: task.id))


def public_task_view(task: AdjudicationTask) -> dict:
    """返回给 reviewer 的盲化视图，刻意不暴露 occurrence/profile/run。"""
    return {
        "id": task.id,
        "case_id": task.case_id,
        "category": task.category,
        "diff": task.diff,
        "issue": task.issue.model_dump(mode="json"),
        "source_context": task.source_context,
    }


def render_review_page(
    bundle: AdjudicationBundle,
    decisions: list[AdjudicationDecision],
    *,
    reviewer_id: str,
    task_id: str = "",
    resolution_mode: bool = False,
) -> str:
    """渲染无 profile 信息的本地盲审页面。"""
    own = {
        decision.task_id: decision
        for decision in decisions
        if decision.reviewer_id == reviewer_id
    }
    task = next((item for item in bundle.tasks if item.id == task_id), None)
    if task is None:
        task = next((item for item in bundle.tasks if item.id not in own), None)
    completed = len(own)
    total = len(bundle.tasks)
    if task is None:
        return (
            "<!doctype html><meta charset='utf-8'><title>Codeguard 盲审</title>"
            f"<h1>本轮已完成</h1><p>{completed}/{total}</p>"
        )

    selected = own.get(task.id)
    labels = (
        ("novel-valid", "额外真实问题"),
        ("invalid", "错误报告"),
        ("duplicate", "重复问题"),
        ("out-of-scope", "超出变更范围"),
        ("uncertain", "暂不确定"),
    )
    radios = "".join(
        "<label><input required type='radio' name='label' value='"
        + value
        + ("' checked>" if selected and selected.label == value else "'>")
        + html.escape(label)
        + "</label>"
        for value, label in labels
    )
    issue = task.issue
    navigation = " ".join(
        f"<a href='/?{urlencode({'reviewer': reviewer_id, 'task': item.id})}'>"
        f"{index + 1}</a>"
        for index, item in enumerate(bundle.tasks)
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Codeguard 人工盲审</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1180px;margin:24px auto;padding:0 18px;color:#18202a}}
.meta{{color:#607080}} pre{{white-space:pre-wrap;background:#f5f7fa;padding:16px;border-radius:8px;overflow:auto}}
label{{display:block;margin:10px 0}} textarea,input[type=text]{{width:100%;box-sizing:border-box}}
.finding{{border-left:4px solid #5865f2;padding:8px 16px;background:#f7f7ff}}
nav{{line-height:2}} button{{padding:10px 18px}}</style></head>
<body>
<h1>Codeguard 人工盲审</h1>
<p class="meta">Reviewer: {html.escape(reviewer_id)} · 已完成 {completed}/{total} ·
任务 {html.escape(task.id)}</p>
<section class="finding"><h2>{html.escape(issue.type)}</h2>
<p><b>{html.escape(issue.file)}:{issue.line}</b> · {html.escape(issue.severity.value)}</p>
<p>{html.escape(issue.message)}</p>
<p>建议：{html.escape(issue.suggestion or "（无）")}</p></section>
<h3>变更</h3><pre>{html.escape(task.diff)}</pre>
{f'<h3>归档证据片段</h3><pre>{html.escape(task.source_context)}</pre>' if task.source_context else ''}
<form method="post" action="/decision">
<input type="hidden" name="task_id" value="{html.escape(task.id)}">
<input type="hidden" name="reviewer_id" value="{html.escape(reviewer_id)}">
<input type="hidden" name="is_resolution" value="{'true' if resolution_mode else 'false'}">
{radios}
<label>重复问题指向的任务 ID<input type="text" name="duplicate_of"
 value="{html.escape(selected.duplicate_of if selected else '')}"></label>
<label>判断依据<textarea name="note" rows="4">{html.escape(selected.note if selected else '')}</textarea></label>
<button type="submit">保存并继续</button></form>
<h3>任务导航</h3><nav>{navigation}</nav>
</body></html>"""


def finalize_decisions(
    bundle: AdjudicationBundle,
    decisions: list[AdjudicationDecision],
    *,
    required_reviewers: int = 2,
) -> FinalizedAdjudication:
    """只有足量 reviewer 完全一致时自动固化；其余进入冲突/缺失队列。"""
    grouped: dict[str, dict[str, AdjudicationDecision]] = defaultdict(dict)
    for decision in decisions:
        grouped[decision.task_id][decision.reviewer_id] = decision

    result = FinalizedAdjudication()
    for task in bundle.tasks:
        task_decisions = list(grouped.get(task.id, {}).values())
        base_decisions = [decision for decision in task_decisions if not decision.is_resolution]
        resolutions = [decision for decision in task_decisions if decision.is_resolution]
        if len(base_decisions) < required_reviewers:
            result.missing.append(task.id)
            continue
        signatures = {
            (decision.label, decision.duplicate_of)
            for decision in base_decisions
        }
        if len(signatures) == 1 and base_decisions[0].label != "uncertain":
            first = base_decisions[0]
            result.resolved[task.id] = ResolvedDecision(
                task_id=task.id,
                label=first.label,
                duplicate_of=first.duplicate_of,
                reviewer_ids=sorted(decision.reviewer_id for decision in base_decisions),
                note=first.note,
            )
            continue
        base_reviewer_ids = {decision.reviewer_id for decision in base_decisions}
        eligible_resolutions = [
            decision
            for decision in resolutions
            if decision.reviewer_id not in base_reviewer_ids
            and decision.label != "uncertain"
        ]
        if not eligible_resolutions:
            result.conflicts.append(task.id)
            continue
        first = eligible_resolutions[-1]
        result.resolved[task.id] = ResolvedDecision(
            task_id=task.id,
            label=first.label,
            duplicate_of=first.duplicate_of,
            reviewer_ids=sorted(base_reviewer_ids | {first.reviewer_id}),
            note=first.note,
        )
    return result


def _canonical_task_id(
    task_id: str,
    resolved: dict[str, ResolvedDecision],
) -> str | None:
    """把 duplicate 链收敛到 novel-valid task；无效类别返回 None。"""
    seen: set[str] = set()
    current = task_id
    while current and current not in seen:
        seen.add(current)
        decision = resolved.get(current)
        if decision is None:
            return None
        if decision.label == "novel-valid":
            return current
        if decision.label != "duplicate":
            return None
        current = decision.duplicate_of
    return None


def rescore_with_adjudication(
    cases: list[EvalCase],
    profile_runs: dict[str, list[list[MatchOutcome]]],
    bundle: AdjudicationBundle,
    finalized: FinalizedAdjudication,
) -> dict[str, list[list[MatchOutcome]]]:
    """在共享 supplemental gold 上重评分，不重新调用审查模型。"""
    unresolved = set(finalized.conflicts) | set(finalized.missing)
    if unresolved:
        raise ValueError(f"仍有未裁决盲审任务:{', '.join(sorted(unresolved))}")

    task_by_id = {task.id: task for task in bundle.tasks}
    occurrence_task: dict[tuple[str, int, str, int], str] = {}
    for task in bundle.tasks:
        for occurrence in task.occurrences:
            occurrence_task[
                (
                    occurrence.profile,
                    occurrence.run_index,
                    occurrence.case_id,
                    occurrence.report_index,
                )
            ] = task.id

    supplemental_by_case: dict[str, set[str]] = defaultdict(set)
    for task_id, task in task_by_id.items():
        canonical = _canonical_task_id(task_id, finalized.resolved)
        if canonical is not None:
            supplemental_by_case[task.case_id].add(canonical)

    rescored = deepcopy(profile_runs)
    for profile, runs in rescored.items():
        for run_index, outcomes in enumerate(runs):
            for outcome in outcomes:
                supplemental = supplemental_by_case.get(outcome.case_id, set())
                outcome.expected_total += len(supplemental)
                outcome.false_negatives += len(supplemental)
                outcome.gold_issue_ids.extend(
                    f"supplemental:{task_id}" for task_id in sorted(supplemental)
                )
                if supplemental:
                    outcome.is_clean = False

                found: set[str] = set()
                outcome.false_positives = 0
                outcome.novel_valid_count = 0
                outcome.duplicate_report_count = 0
                outcome.invalid_report_count = 0
                outcome.out_of_scope_count = 0
                for report_index in outcome.unmatched_report_indices:
                    occurrence_task_id = occurrence_task.get(
                        (profile, run_index, outcome.case_id, report_index)
                    )
                    decision = finalized.resolved.get(occurrence_task_id or "")
                    canonical = (
                        _canonical_task_id(occurrence_task_id, finalized.resolved)
                        if occurrence_task_id
                        else None
                    )
                    if canonical is not None:
                        if canonical in found:
                            outcome.duplicate_report_count += 1
                            outcome.false_positives += 1
                            continue
                        found.add(canonical)
                        outcome.novel_valid_count += 1
                        outcome.true_positives += 1
                        outcome.false_negatives -= 1
                        outcome.detected_issue_ids.append(f"supplemental:{canonical}")
                        continue
                    if decision is not None and decision.label == "out-of-scope":
                        outcome.out_of_scope_count += 1
                    else:
                        outcome.invalid_report_count += 1
                    outcome.false_positives += 1
    return rescored


def serve_adjudication(
    bundle_path: Path,
    decisions_path: Path,
    *,
    reviewer_id: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    resolution_mode: bool = False,
) -> None:
    """启动只绑定本机的轻量盲审服务。"""
    bundle = load_bundle(bundle_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            requested_reviewer = (query.get("reviewer") or [reviewer_id])[0]
            task_id = (query.get("task") or [""])[0]
            page = render_review_page(
                bundle,
                load_decisions(decisions_path),
                reviewer_id=requested_reviewer,
                task_id=task_id,
                resolution_mode=resolution_mode,
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/decision":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = min(int(self.headers.get("Content-Length", "0")), 64 * 1024)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            try:
                decision = AdjudicationDecision(
                    task_id=form["task_id"][0],
                    reviewer_id=form["reviewer_id"][0],
                    label=cast(AdjudicationLabel, form["label"][0]),
                    duplicate_of=(form.get("duplicate_of") or [""])[0],
                    note=(form.get("note") or [""])[0],
                    is_resolution=(form.get("is_resolution") or ["false"])[0]
                    == "true",
                )
            except (KeyError, ValueError) as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            save_decision(decisions_path, decision)
            location = "/?" + urlencode({"reviewer": decision.reviewer_id})
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Codeguard 盲审工作台: http://{host}:{port}/?reviewer={reviewer_id}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.adjudication")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动本地盲审工作台")
    serve.add_argument("--bundle", required=True, type=Path)
    serve.add_argument("--decisions", required=True, type=Path)
    serve.add_argument("--reviewer", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--resolution",
        action="store_true",
        help="以争议仲裁模式记录可覆盖双人分歧的最终决定",
    )
    finalize = sub.add_parser("finalize", help="固化一致裁决")
    finalize.add_argument("--bundle", required=True, type=Path)
    finalize.add_argument("--decisions", required=True, type=Path)
    finalize.add_argument("--output", required=True, type=Path)
    finalize.add_argument("--required-reviewers", type=int, default=2)
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve_adjudication(
            args.bundle,
            args.decisions,
            reviewer_id=args.reviewer,
            host=args.host,
            port=args.port,
            resolution_mode=args.resolution,
        )
        return 0
    result = finalize_decisions(
        load_bundle(args.bundle),
        load_decisions(args.decisions),
        required_reviewers=args.required_reviewers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"resolved={len(result.resolved)} conflicts={len(result.conflicts)} "
        f"missing={len(result.missing)}"
    )
    return 2 if result.conflicts or result.missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

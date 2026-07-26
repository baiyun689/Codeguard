"""准备可复现的 repo-backed 面试评测集。

版本库只保存轻量 manifest；第三方 Git 对象和 checkout 均进入显式 cache/output，
不把上游项目源码复制进 Codeguard Git 历史。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from evals.dataset import load_cases


class SuiteCaseSpec(BaseModel):
    id: str
    source: str
    repository_url: str
    fix_revision: str
    parent_revision: str = ""
    direction: str = Field(pattern="^(reversed-fix|forward-clean)$")
    category: str
    dimension: str = "logic"
    ground_truth_mode: str = "known-issue-only"
    difficulty: str = "standard"
    capability: list[str] = Field(default_factory=lambda: ["whole-file"])
    license: str = ""
    cve: str = ""
    expected: dict | None = None


class SuiteManifest(BaseModel):
    version: str
    cases: list[SuiteCaseSpec]


def load_suite_manifest(path: Path) -> SuiteManifest:
    return SuiteManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _run_git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知 Git 错误"
        raise RuntimeError(f"git {' '.join(args)} 失败: {detail}")
    return result.stdout


def _cache_key(repository_url: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9._-]+", "-", repository_url).strip("-")[-80:]
    digest = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}.git"


def _ensure_revision_cache(spec: SuiteCaseSpec, cache_root: Path) -> tuple[Path, str]:
    repositories = cache_root / "repositories"
    repositories.mkdir(parents=True, exist_ok=True)
    mirror = repositories / _cache_key(spec.repository_url)
    if not mirror.is_dir():
        _run_git("init", "--bare", str(mirror))
    try:
        configured_origin = _run_git(
            "--git-dir", str(mirror), "config", "--get", "remote.origin.url"
        ).strip()
    except RuntimeError:
        _run_git("--git-dir", str(mirror), "remote", "add", "origin", spec.repository_url)
    else:
        if configured_origin != spec.repository_url:
            _run_git(
                "--git-dir",
                str(mirror),
                "remote",
                "set-url",
                "origin",
                spec.repository_url,
            )

    revisions = [spec.fix_revision]
    if spec.parent_revision:
        revisions.append(spec.parent_revision)
    _run_git(
        "--git-dir",
        str(mirror),
        "fetch",
        "--force",
        "--no-tags",
        "--depth=2",
        "origin",
        *revisions,
    )
    fixed = _run_git(
        "--git-dir", str(mirror), "rev-parse", spec.fix_revision
    ).strip()
    parent = spec.parent_revision or f"{fixed}^"
    parent = _run_git("--git-dir", str(mirror), "rev-parse", parent).strip()
    # 浅抓 fix 时 Git 可能只留下 parent 的 commit 对象而省略其 tree；rev-parse
    # 仍会成功，但随后构造 vulnerable snapshot 会在 checkout 时失败。
    # 显式补抓 parent，保证精确版本不仅“可命名”，而且完整可检出。
    _run_git(
        "--git-dir",
        str(mirror),
        "fetch",
        "--force",
        "--no-tags",
        "--depth=1",
        "origin",
        parent,
    )
    return mirror, parent


def _first_changed_java_file(diff: str) -> str:
    """只推导文件范围；没有人工锚点时绝不伪造根因行号。"""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            normal = current_file.lower()
            if (
                current_file.endswith(".java")
                and "/src/test/" not in f"/{normal}"
                and "/test/" not in f"/{normal}"
            ):
                return current_file
    return ""


def _java_hunk_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = ""
    for row in diff.splitlines():
        if row.startswith("+++ b/"):
            current_file = row[6:]
            continue
        if not row.startswith("@@") or not current_file.endswith(".java"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", row)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        ranges.setdefault(current_file, []).append((start, start + max(count - 1, 0)))
    return ranges


def _prepare_git_case(
    spec: SuiteCaseSpec,
    output_root: Path,
    cache_root: Path,
) -> None:
    case_dir = output_root / "repo" / spec.id
    case_file = case_dir / "case.yaml"
    snapshot = case_dir / "repo"
    if case_file.is_file() and snapshot.is_dir():
        return
    case_dir.mkdir(parents=True, exist_ok=True)

    mirror, parent = _ensure_revision_cache(spec, cache_root)
    fixed = _run_git(
        "--git-dir", str(mirror), "rev-parse", spec.fix_revision
    ).strip()
    if spec.direction == "reversed-fix":
        base_revision, head_revision = fixed, parent
    else:
        base_revision, head_revision = parent, fixed
    diff = _run_git(
        "--git-dir",
        str(mirror),
        "diff",
        "--binary",
        "--find-renames",
        base_revision,
        head_revision,
        "--",
        "*.java",
    )
    if not diff.strip():
        raise ValueError(f"{spec.id}:所选修复没有 Java diff")

    if not snapshot.is_dir():
        _run_git(
            "clone",
            "--no-checkout",
            "--shared",
            str(mirror),
            str(snapshot),
        )
    # bare cache 只保存按 SHA 抓取的悬空提交，没有默认 ref；local clone 因此可能
    # 创建出空 worktree 且不建立 alternates。显式从本地 cache 抓目标 SHA，
    # 同时也可修复上次中断留下的不完整 snapshot。
    _run_git(
        "fetch",
        "--force",
        "--no-tags",
        "--depth=1",
        "origin",
        head_revision,
        cwd=snapshot,
    )
    _run_git("checkout", "--detach", "--force", head_revision, cwd=snapshot)

    (case_dir / "changes.diff").write_text(diff, encoding="utf-8")
    expected_rows: list[dict] = []
    if spec.direction == "reversed-fix":
        expected = dict(spec.expected or {})
        expected.setdefault("id", "E1")
        expected.setdefault("type_keywords", [spec.category])
        expected.setdefault("file", _first_changed_java_file(diff))
        expected.setdefault("line", 0)
        expected.setdefault("tolerance", 0)
        expected_rows.append(expected)

    description = (
        f"{spec.source} {spec.direction} case; "
        f"fixed={fixed[:12]} vulnerable={parent[:12]}"
    )
    if spec.cve:
        description += f"; {spec.cve}"
    raw = {
        "id": spec.id,
        "category": spec.category if expected_rows else "clean",
        "dimension": spec.dimension,
        "description": description,
        "ground_truth_mode": spec.ground_truth_mode,
        "difficulty": spec.difficulty,
        "capability": spec.capability,
        "provenance": {
            "source": spec.source,
            "repository_url": spec.repository_url,
            "base_revision": base_revision,
            "head_revision": head_revision,
            "patch_direction": spec.direction,
            "license": spec.license,
        },
        "expected": expected_rows,
    }
    case_file.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _select_specs(
    manifest: SuiteManifest,
    case_ids: set[str] | None,
    limit: int | None,
) -> list[SuiteCaseSpec]:
    specs = manifest.cases
    if case_ids:
        known = {spec.id for spec in specs}
        unknown = case_ids - known
        if unknown:
            raise ValueError(f"manifest 中不存在用例:{', '.join(sorted(unknown))}")
        specs = [spec for spec in specs if spec.id in case_ids]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        specs = specs[:limit]
    return specs


def prepare_suite(
    manifest_path: Path,
    output_root: Path,
    cache_root: Path,
    *,
    case_ids: set[str] | None = None,
    limit: int | None = None,
) -> list:
    manifest = load_suite_manifest(manifest_path)
    specs = _select_specs(manifest, case_ids, limit)
    for spec in specs:
        _prepare_git_case(spec, output_root, cache_root)
    return validate_prepared_suite(
        manifest_path,
        output_root,
        case_ids=case_ids,
        limit=limit,
    )


def validate_prepared_suite(
    manifest_path: Path,
    output_root: Path,
    *,
    case_ids: set[str] | None = None,
    limit: int | None = None,
) -> list:
    """校验 prepared suite 的数量、方向、快照 SHA 与标答基本可定位性。"""
    manifest = load_suite_manifest(manifest_path)
    specs = _select_specs(manifest, case_ids, limit)
    spec_by_id = {spec.id: spec for spec in specs}
    for spec in specs:
        diff_path = output_root / "repo" / spec.id / "changes.diff"
        if not diff_path.is_file() or not diff_path.read_text(
            encoding="utf-8", errors="replace"
        ).strip():
            raise ValueError(f"{spec.id}:diff 为空")
    cases = [case for case in load_cases(output_root) if case.id in spec_by_id]
    if len(cases) != len(specs):
        raise ValueError(
            f"suite 数量不一致:manifest={len(specs)} prepared={len(cases)}"
        )
    for case in cases:
        spec = spec_by_id[case.id]
        if not case.diff.strip():
            raise ValueError(f"{case.id}:diff 为空")
        snapshot = Path(case.repo_path)
        if not snapshot.is_dir():
            raise ValueError(f"{case.id}:repo 快照不存在")
        head = _run_git("rev-parse", "HEAD", cwd=snapshot).strip()
        if head != case.provenance.head_revision:
            raise ValueError(f"{case.id}:repo HEAD 与 provenance 不一致")
        if case.provenance.repository_url != spec.repository_url:
            raise ValueError(f"{case.id}:repository_url 与 manifest 不一致")
        if case.provenance.patch_direction != spec.direction:
            raise ValueError(f"{case.id}:patch direction 与 manifest 不一致")
        if spec.direction == "reversed-fix":
            expected_base = spec.fix_revision
            expected_head = spec.parent_revision
        else:
            expected_base = spec.parent_revision
            expected_head = spec.fix_revision
        if expected_base and case.provenance.base_revision != expected_base:
            raise ValueError(f"{case.id}:base revision 与 manifest 不一致")
        if expected_head and case.provenance.head_revision != expected_head:
            raise ValueError(f"{case.id}:head revision 与 manifest 不一致")
        if spec.direction == "reversed-fix":
            if len(case.expected) != 1:
                raise ValueError(f"{case.id}:反向修复用例必须有一个已知根因")
            expected = case.expected[0]
            if not expected.file or not expected.root_cause:
                raise ValueError(f"{case.id}:标答缺少文件或根因")
            hunk_ranges = _java_hunk_ranges(case.diff)
            if expected.file not in hunk_ranges:
                raise ValueError(f"{case.id}:标答文件不在 Java diff 中")
            if expected.line > 0:
                if not any(start <= expected.line <= end for start, end in hunk_ranges[expected.file]):
                    raise ValueError(f"{case.id}:标答行号不在新侧 diff hunk 中")
                source = snapshot / expected.file
                if not source.is_file():
                    raise ValueError(f"{case.id}:标答文件不在 HEAD 快照中")
                line_count = len(source.read_text(encoding="utf-8", errors="replace").splitlines())
                if expected.line > line_count:
                    raise ValueError(f"{case.id}:标答行号超出 HEAD 文件")
            manifest_expected = spec.expected or {}
            for field in ("root_cause", "cwe", "risk_tag"):
                if getattr(expected, field) != str(manifest_expected.get(field, "")):
                    raise ValueError(f"{case.id}:标答 {field} 与 manifest 不一致")
        elif case.expected:
            raise ValueError(f"{case.id}:forward-clean 不应携带已知缺陷标答")
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.interview_suite")
    parser.add_argument("command", choices=["prepare", "validate"])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="只准备指定 case id；可重复传入",
    )
    parser.add_argument("--limit", type=int, default=None, help="只处理清单前 N 条")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        cases = prepare_suite(
            args.manifest,
            args.output,
            args.cache,
            case_ids=set(args.case) or None,
            limit=args.limit,
        )
    else:
        cases = validate_prepared_suite(
            args.manifest,
            args.output,
            case_ids=set(args.case) or None,
            limit=args.limit,
        )
    print(f"validated_cases={len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

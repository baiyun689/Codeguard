"""数据集加载:把 dataset/ 下的用例读成 EvalCase 列表。

两类用例并存,合并为同一列表:

1. **内联合成用例**:dataset/vuln/*.yaml 与 dataset/clean/*.yaml,每个文件一条,
   diff 直接内联在 YAML 里(磁盘无对应文件,工具读不到)。新增 = 丢一个 YAML。

2. **外置 diff 合成用例**:任意分类目录下的 `<case_id>/case.yaml` +
   `changes.diff`,用于较长 diff 与标答分离,不提供工具仓库快照。

3. **repo-backed 自包含快照用例**:dataset/repo/<case_id>/ 一个目录,含
   - repo/         变更后的最小可解析工程(工具据此能读到 diff 之外的上下文)
   - changes.diff  被审查的 unified diff
   - case.yaml     标答 + 能力标签等元数据(diff 由 changes.diff 提供,可不在此内联)
   加载时把 changes.diff 注入 diff 字段、把 repo/ 的绝对路径写入 repo_path。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from evals.schema import EvalCase

_DATASET_DIR = Path(__file__).resolve().parent / "dataset"

# repo-backed 用例的顶层目录名(相对数据集根),与内联用例区隔。
_REPO_SUBDIR = "repo"
_CASE_FILE = "case.yaml"
_DIFF_FILE = "changes.diff"


def _load_synthetic_cases(root: Path) -> list[EvalCase]:
    """加载内联合成用例:扫 *.yaml,但跳过 repo-backed 区域(dataset/repo/**)。"""
    cases: list[EvalCase] = []
    repo_root = root / _REPO_SUBDIR
    for path in sorted(root.rglob("*.yaml")):
        # 跳过 repo-backed 区域里的任何 yaml(case.yaml、工程里的 application.yaml 等)。
        if repo_root in path.parents or path == repo_root:
            continue
        if path.name == _CASE_FILE:
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        cases.append(EvalCase.model_validate(raw))
    return cases


def _load_external_diff_cases(root: Path) -> list[EvalCase]:
    """加载分类目录中的 case.yaml + changes.diff 合成用例。"""
    repo_root = root / _REPO_SUBDIR
    cases: list[EvalCase] = []
    for case_file in sorted(root.rglob(_CASE_FILE)):
        if repo_root in case_file.parents:
            continue
        case_dir = case_file.parent
        diff_file = case_dir / _DIFF_FILE
        if not diff_file.is_file():
            raise ValueError(f"外置 diff 用例缺少 {_DIFF_FILE}:{case_dir}")
        raw = yaml.safe_load(case_file.read_text(encoding="utf-8")) or {}
        raw["diff"] = diff_file.read_text(encoding="utf-8")
        cases.append(EvalCase.model_validate(raw))
    return cases


def _load_repo_backed_cases(root: Path) -> list[EvalCase]:
    """加载 repo-backed 用例:遍历 dataset/repo/<case_id>/,注入 diff 与 repo_path。"""
    repo_root = root / _REPO_SUBDIR
    if not repo_root.is_dir():
        return []

    cases: list[EvalCase] = []
    for case_dir in sorted(p for p in repo_root.iterdir() if p.is_dir()):
        case_file = case_dir / _CASE_FILE
        if not case_file.is_file():
            continue  # 不是合法用例目录,跳过

        raw = yaml.safe_load(case_file.read_text(encoding="utf-8")) or {}

        # diff 优先取 changes.diff 文件;否则回退到 case.yaml 内联的 diff。
        diff_file = case_dir / _DIFF_FILE
        if diff_file.is_file():
            raw["diff"] = diff_file.read_text(encoding="utf-8")
        if not raw.get("diff"):
            raise ValueError(f"repo-backed 用例缺少 diff:{case_dir}(需 {_DIFF_FILE} 或内联 diff)")

        # repo_path 指向变更后的工程快照目录(绝对路径,供工具读取)。
        snapshot = case_dir / _REPO_SUBDIR
        if not snapshot.is_dir():
            raise ValueError(f"repo-backed 用例缺少 {_REPO_SUBDIR}/ 快照目录:{case_dir}")
        raw["repo_path"] = str(snapshot.resolve())

        # 未标注能力时,repo-backed 用例默认按 file 能力归类(造它就是为了让工具读文件);
        # 已显式标注则尊重标注。
        raw.setdefault("capability", ["file"])

        cases.append(EvalCase.model_validate(raw))
    return cases


def load_cases(dataset_dir: Path | None = None) -> list[EvalCase]:
    """加载数据集下所有用例(内联 + repo-backed),按 id 排序返回。"""
    root = dataset_dir or _DATASET_DIR
    cases = (
        _load_synthetic_cases(root)
        + _load_external_diff_cases(root)
        + _load_repo_backed_cases(root)
    )
    if not cases:
        raise FileNotFoundError(f"在 {root} 下没找到任何用例(*.yaml 或 {_REPO_SUBDIR}/<case>/)")
    cases.sort(key=lambda c: c.id)
    return cases

"""数据集加载:把 dataset/ 下的 YAML 用例读成 EvalCase 列表。

约定:dataset/vuln/*.yaml 与 dataset/clean/*.yaml,每个文件一条用例。
新增用例 = 往对应目录丢一个 YAML,无需改代码。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from evals.schema import EvalCase

_DATASET_DIR = Path(__file__).resolve().parent / "dataset"


def load_cases(dataset_dir: Path | None = None) -> list[EvalCase]:
    """加载数据集下所有用例,按 id 排序返回。"""
    root = dataset_dir or _DATASET_DIR
    cases: list[EvalCase] = []
    for path in sorted(root.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        cases.append(EvalCase.model_validate(raw))
    if not cases:
        raise FileNotFoundError(f"在 {root} 下没找到任何 *.yaml 用例")
    return cases

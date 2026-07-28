# representative-multi-v1

该数据集从冻结的 `interview-v1` 中选出 10 个不同真实 Java 项目。每个 case
保留原始 reversed-fix 问题，并在完整项目快照中加入两个彼此独立的受控问题。

- `repo/`：PR 后完整项目快照。
- `changes.diff`：原始真实 diff 与新增受控 diff 的有效 Git unified diff。
- `seeded.diff`：仅包含两个新增问题，便于审计数据构造，不向审查模型单独暴露。
- `ground-truth.yaml`：三条完整金标。
- `oracle-tests/`：触发条件与可观察结果契约，不放入 `repo/`。

源码中不加入说明问题性质的注释。该基准用于受控项目级评测，不宣称新增问题来自
真实上游 PR。

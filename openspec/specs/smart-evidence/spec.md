# smart-evidence Specification

## Purpose
定义 evidence_agent 升级为智能举证节点的行为契约。evidence_agent 不再只是确定性工具调用转发器，而是使用 LLM 分析工具输出的语义含义，产出结构化的证据判定。

## Requirements

### Requirement: LLM 证据分析
evidence_agent SHALL 在获取工具原始输出后，对每个 EvidenceRequest 调用一次 LLM 分析证据含义。LLM 分析 SHALL 产出以下结构化判定之一：SUPPORTS（工具输出支持候选主张）、CONTRADICTS（工具输出反驳候选主张）、INSUFFICIENT（工具输出不足以判断）。

#### Scenario: 工具输出支持候选主张
- **WHEN** 工具返回的代码片段证实了候选问题的存在（如 find_callers 确认调用方未做 null 检查）
- **THEN** evidence_agent 的 LLM 分析产出 SUPPORTS 判定，并附带推理依据

#### Scenario: 工具输出反驳候选主张
- **WHEN** 工具返回的代码片段显示候选问题不成立（如 get_file_content 显示已有判空保护、已有 try-with-resources）
- **THEN** evidence_agent 的 LLM 分析产出 CONTRADICTS 判定，并附带推理依据

#### Scenario: 工具输出不足以判断
- **WHEN** 工具返回的信息与候选主张相关但不足以支持或反驳（如 get_code_metrics 返回了方法复杂度但候选主张是关于命名的）
- **THEN** evidence_agent 的 LLM 分析产出 INSUFFICIENT 判定，并附带推理依据

### Requirement: 结构化证据记录
evidence_agent SHALL 将 LLM 分析结果写入 EvidenceNote，包括 supports（支持证据列表，每条带推理）、contradicts（反驳证据列表，每条带推理）、unknowns（证据不足项）。EvidenceNote.status SHALL 根据 supports/contradicts 的分布自动计算：有 supports 无 contradicts → "supported"；有 contradicts 无 supports → "contradicted"；两者都有 → "mixed"；两者都无 → "insufficient"。

#### Scenario: EvidenceNote 携带推理
- **WHEN** evidence_agent 完成证据分析
- **THEN** 每条 supports/contradicts/unknowns 条目 SHALL 包含推理依据，不只是 raw output 片段

#### Scenario: status 自动计算
- **WHEN** 一个候选的所有证据都是 INSUFFICIENT
- **THEN** EvidenceNote.status SHALL 为 "insufficient"

### Requirement: 证据去重与预算
evidence_agent SHALL 对同一（工具名, 参数）组合只调用一次 Java 工具。evidence_agent SHALL 遵守全局 EvidenceRequest 数量上限（MAX_TOTAL_EVIDENCE_REQUESTS = 20）。

#### Scenario: 重复工具调用被跳过
- **WHEN** 两个 EvidenceRequest 请求相同的工具和参数
- **THEN** 只执行第一次工具调用，第二次复用结果

### Requirement: 回退路径
当 LLM 不可用或证据分析 LLM 调用失败时，evidence_agent SHALL 回退到当前确定性行为：直接将原始工具输出截取前 200 字符作为 supports，状态标记为 "mixed"。

#### Scenario: LLM 分析失败时回退
- **WHEN** 证据分析 LLM 调用失败或返回无效结果
- **THEN** evidence_agent 不中断管线，回退到 raw output 模式，EvidenceNote 标记为 "mixed"

### Requirement: 语义证据能力

EvidenceStrategy SHALL 声明语义证据能力并由 EvidenceAgent 映射到图谱工具。图谱返回 `unknown`、partial、超时或无效状态时，EvidenceNote SHALL 标记为 insufficient，不得作为支持或反证。

# find-callers Specification

## Purpose

提供 `find_callers` 工具,接收文件路径和方法名作为输入,基于 JavaParser 提取的 ref 数据做反向索引查询,返回全仓库所有直接调用方列表(精确到文件+行号+代码片段)。该工具回答"改了这个方法会影响谁"这个逻辑正确性问题,仅分配给 logic 审查员。

## Requirements

### Requirement: find_callers 工具查询方法调用方

系统 SHALL 提供 `find_callers` 工具。输入为"文件路径#方法名"(如 `src/main/java/com/example/OrderService.java#calculatePrice`),输出为该方法在**全仓库范围内**(不限 diff 改动文件集合)的所有直接调用方列表,每条包含(调用文件路径, 行号, 调用代码片段)。工具 SHALL 经统一 `POST /api/v1/tools/{name}` 协议接入,与其他工具同构。

#### Scenario: 查询存在调用方的方法

- **WHEN** 审查员调用 `find_callers("src/main/java/OrderService.java#calculatePrice")`
- **THEN** 返回所有调用 `calculatePrice` 的位置列表,每条包含文件+行号+调用代码片段

#### Scenario: 查询无调用方的方法

- **WHEN** 审查员调用 `find_callers` 查询一个未被任何代码调用的私有方法
- **THEN** 返回空列表并附说明"未找到直接调用方"

#### Scenario: 查询不存在的方法

- **WHEN** 审查员调用 `find_callers` 查询一个不存在的方法名
- **THEN** 返回结构化错误,说明未找到该方法的定义标签

### Requirement: 调用方查询基于 JavaParser 标签反向索引

工具 SHALL 复用 `JavaTagExtractor` 已提取的 ref(方法调用)数据,构建"被调方法全限定名 → [(调用文件, 行号, 代码片段)]"的反向索引。查询时 SHALL 将输入参数解析为全限定名后在索引中查找。查询 SHALL 为确定性:同一输入同一仓库同一时刻产出相同结果。

#### Scenario: 反向索引匹配

- **WHEN** 仓库中 `PaymentGateway.java:234` 调用了 `orderService.calculatePrice(items)`
- **THEN** 查询 `calculatePrice` 时返回的记录包含 `PaymentGateway.java:234` 及调用代码片段

#### Scenario: 同文件内部调用也被收录

- **WHEN** `OrderService.java:156` 内部调用了同类的 `calculatePrice`
- **THEN** `find_callers` 返回的结果包含该同文件内部调用

### Requirement: find_callers 仅 logic 审查员可调用

`find_callers` SHALL 仅出现在 logic 审查员的 `tool_allowlist` 中。security 和 quality 审查员的工具集 SHALL NOT 包含此工具。

#### Scenario: logic 可调用

- **WHEN** logic 审查员以 ReAct 模式运行
- **THEN** 其可用工具集中包含 `find_callers`

#### Scenario: security 和 quality 不可调用

- **WHEN** security 或 quality 审查员以 ReAct 模式运行
- **THEN** 其可用工具集中不包含 `find_callers`

### Requirement: Python 侧工具定义

Python 侧 SHALL 提供 `find_callers` 的 LangChain 工具定义(`make_callers_tool`),入参为"文件路径#方法名"格式的字符串。工具描述 SHALL 写成动作触发式:"当你发现一个方法的签名/返回值被修改、需要确认哪些调用方可能受影响时调用。入参格式:'文件路径#方法名'。"

#### Scenario: 工具挂入 ReAct 工具集

- **WHEN** logic 审查员以 ReAct Agent 形态运行
- **THEN** `find_callers` 出现在其可用工具列表中

### Requirement: 语义图兼容迁移

`find_callers` SHALL 作为兼容 Adapter 查询当前 revision 的 `ProjectCodeGraph`，不得逐次重新扫描仓库或按简单方法名匹配。默认 BehaviorAgent SHALL 使用 `inspect_change_impact(symbol_id)`；找不到关系时 SHALL 区分 `not_found` 与 `unknown`。

# GitHub Check Run 反馈加固设计

## 背景

PR 审查已能获取 installation token 并创建 Check Run，但完成 Check Run 的 PATCH 请求收到 GitHub HTML 400；紧接着的无 annotations 降级请求又遇到 `Connection reset`。现有实现还会把所有 LLM issue 原样转换为 annotation，只把非正行号抬到 1，没有确认文件与行号是否位于本次 PR diff。

## 决策

1. `GitHubClient` 为所有 GitHub REST 请求统一设置 `Accept`、`Content-Type`、`X-GitHub-Api-Version` 和 `User-Agent`，避免各端点形成不同的 HTTP 契约。
2. `GitHubClient` 只对可安全重放的请求做有限重试：网络 `IOException` 与 HTTP 502/503/504 最多重试一次；4xx 不重试。重试不刷新业务载荷，也不把确定性参数错误伪装成瞬态故障。
3. 非成功响应记录状态码、`X-GitHub-Request-Id`、响应 `Content-Type` 和受限长度的请求/响应摘要；不记录 Authorization/token。
4. `ResultFeedback` 只为能够映射到当前 unified diff new-side 的 issue 创建 annotation。无法定位的问题不阻塞 Check Run 摘要，CRITICAL 行级反馈继续沿用现有普通 PR 评论降级。
5. annotations 请求失败后仍尝试无 annotations 完成，但底层瞬态重试由 `GitHubClient` 统一处理，调用层不复制重试策略。

## 边界

- 不改变产品 `Issue`、ReviewJob 数据库结构或 GitHub App 权限。
- 不增加无限重试、annotations 二分上传或新的后台队列。
- 保留当前工作树已有的秒级 `completed_at`、summary 截断和 annotation message 截断改动，并用测试覆盖其相关边界。

## 测试

- 本地 HTTP server 捕获真实 Java `HttpRequest`，验证 PATCH 方法、统一请求头、JSON body 和一次瞬态重试。
- `ResultFeedback` 单元测试覆盖：合法新增行保留、非 diff 行/未知文件/删除行过滤、空 diff 不生成 annotations。
- 运行 Gateway 全量 Maven 测试，确认既有 GitHub JWT、行号映射和 CI 执行测试无回归。

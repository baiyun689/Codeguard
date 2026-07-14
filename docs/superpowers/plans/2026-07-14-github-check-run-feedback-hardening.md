# GitHub Check Run Feedback Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 PR 审查稳定完成 GitHub Check Run，并阻止非法 annotation 拖垮整批反馈。

**Architecture:** `GitHubClient` 负责统一 HTTP 契约、可重放请求的有限瞬态重试和安全诊断；`ResultFeedback` 负责把产品 issue 投影为仅限当前 diff new-side 的 annotations。调用层保留带 annotations → 无 annotations 的业务降级，但不自行实现网络重试。

**Tech Stack:** Java 17、JDK `HttpClient`、Jackson、JUnit 5、JDK `HttpServer`、Maven

## Global Constraints

- 不改变产品 `Issue`、ReviewJob 数据库结构或 GitHub App 权限。
- 网络 `IOException` 与 HTTP 502/503/504 最多重试一次，4xx 不重试。
- 不记录 Authorization 或 installation token。
- 保留工作树中与本故障有关的秒级时间戳、summary/message 截断改动。

---

### Task 1: GitHub REST 请求契约与瞬态重试

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/ci/github/GitHubClient.java`
- Modify: `services/gateway/src/test/java/com/codeguard/ci/github/GitHubClientTest.java`

**Interfaces:**
- Consumes: `HttpRequest` 与 `HttpResponse<String>`
- Produces: 统一构建请求的私有 helper，以及只重试一次的发送 helper

- [x] **Step 1: 写失败测试**

用本地 `HttpServer` 捕获请求，断言 JSON PATCH 包含 `Content-Type: application/json; charset=utf-8`、`X-GitHub-Api-Version: 2022-11-28`、`User-Agent: Codeguard`；服务第一次返回 503、第二次 200，断言总请求数为 2。另测 400 只请求一次。

- [x] **Step 2: 验证测试因缺少可注入 API/HTTP seam 而失败**

Run: `mvn -Dtest=GitHubClientTest test`

Expected: FAIL，现有构造函数固定 `api.github.com` 且无统一请求/重试 seam。

- [x] **Step 3: 最小实现**

增加包内测试构造 seam；所有请求通过统一 builder 添加固定 headers；发送 helper 在 `IOException` 或 502/503/504 时最多再发送一次，新建请求但复用不可变 body；错误日志附 `X-GitHub-Request-Id` 与响应类型。

- [x] **Step 4: 验证单测转绿**

Run: `mvn -Dtest=GitHubClientTest test`

Expected: PASS

### Task 2: Annotation diff 合法性过滤

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/ci/executor/ResultFeedback.java`
- Modify: `services/gateway/src/test/java/com/codeguard/ci/executor/ResultFeedbackLineMappingTest.java`

**Interfaces:**
- Consumes: `ReviewJob.diffText`、issue `file/line`
- Produces: `buildAnnotations(List<JsonNode>, String diffText)`，只返回 diff new-side 可定位项

- [x] **Step 1: 写失败测试**

通过包内纯函数 seam 输入包含合法新增行、删除行、hunk 外行和未知文件的 issues，断言仅合法 new-side 行生成 annotation。

- [x] **Step 2: 验证测试失败**

Run: `mvn -Dtest=ResultFeedbackLineMappingTest test`

Expected: FAIL，现有 `buildAnnotations` 不消费 diff，全部 issue 都被发送。

- [x] **Step 3: 最小实现**

让 annotation 构建复用统一 diff 定位逻辑；只保留 `mapToDiffLine(...) > 0` 的 issue，并记录过滤数量，不改变 summary 与行级评论降级。

- [x] **Step 4: 验证单测转绿**

Run: `mvn -Dtest=ResultFeedbackLineMappingTest test`

Expected: PASS

### Task 3: 全量验证与提交

**Files:**
- Verify: `services/gateway`
- Update: `DECISIONS.md`

**Interfaces:**
- Consumes: Task 1–2 的稳定行为
- Produces: 可追溯的故障复盘和当前分支 commit

- [x] **Step 1: 运行 Gateway 全量测试**

Run: `mvn test`

Expected: BUILD SUCCESS，0 failures/errors

- [x] **Step 2: 检查差异与敏感信息**

Run: `git diff --check` 以及 `rg -n "Authorization.*log|installation token.*token" services/gateway/src/main/java`

Expected: 无空白错误；日志不输出 token。

- [x] **Step 3: 记录决策并提交**

在 `DECISIONS.md` 记录根因边界、重试分类和 annotation 过滤；仅暂存本次修复相关文件，提交：

```text
fix(ci): 加固 GitHub Check Run 反馈
```

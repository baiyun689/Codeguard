# CI PR 行级评论行号映射修复

**日期**: 2026-07-07
**状态**: 设计完成，待实现

---

## 概述

修复 CI 反馈阶段行级评论 422 失败的问题。根因：LLM 报的 `line` 是源文件绝对行号，GitHub API 要求行号落在 PR diff 的 hunk 上下文行内。当前只做了降级兜底（失败的 issue 汇总为 PR 普通评论），本 change 实现根因修复——将绝对行号映射为 diff 内的行号。

---

## 当前问题

```
LLM 报 issue.line = 126 (源文件绝对行号)
         │
         ▼
GitHub API: POST /repos/.../pulls/.../comments { path, line: 126 }
         │
         ▼
GitHub: 126 这个行号在 PR diff 中找不到 → 422 "could not be resolved"
         │
         ▼
当前处理: catch → log.warn → 降级为 PR 普通评论
```

## 目标行为

```
LLM 报 issue.line = 126 (源文件绝对行号)
         │
         ▼
mapToDiffLine(diffText, file, 126) → diffLine = 15 或 -1
         │                │
    diffLine > 0          diffLine = -1
         │                     │
         ▼                     ▼
POST { line: 15 }         降级为 PR 普通评论
         │
         ▼
      201 ✅ 评论成功贴在 diff 的正确行上
```

---

## 架构改动

### 数据流

```
ReviewExecutorImpl                    ReviewJob                  ResultFeedback
  git diff base...HEAD ──→ job.setDiffText(diffText) ──→ feedback.postResults(job)
                               │                              │
                               ▼                              ▼
                         H2: review_jobs             读取 job.getDiffText()
                         + diff_text CLOB             解析 unified diff
                                                     mapToDiffLine(file, absLine)
                                                          │
                                                     diffLine > 0 → line 评论
                                                     diffLine = -1 → 降级普通评论
```

### 改动文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `ReviewJob.java` | +`diffText` 字段 | +getter/setter/dbSetter |
| `JobRepository.java` | DDL/DML 加列 | `diff_text CLOB`；INSERT/UPDATE/mapRow |
| `ReviewExecutorImpl.java` | 存 diff 文本 | clone 后 `git diff base...HEAD` → `job.setDiffText()` |
| `ResultFeedback.java` | +`mapToDiffLine()` | 解析 unified diff，绝对行号→diff 行号映射 |
| `GitHubClient.java` | 不改 | 上一版已改为返回 boolean + 降级逻辑 |

### `JobRepository` DDL 变更

```sql
-- review_jobs 表新增一列
ALTER TABLE review_jobs ADD COLUMN diff_text CLOB;
-- 新建表时直接在 CREATE TABLE 中包含
```

---

## 映射算法

```
mapToDiffLine(String diffText, String targetFile, int absoluteLine) → int:

  1. 按文件拆分 diff
     分隔符: "diff --git a/" + targetFile（匹配该文件在 diff 中的块）

  2. 遍历 hunk header
     正则: @@ -\d+(?:,\d+)? +(\d+)(?:,\d+)? @@
     newLine = 提取的 +newStart

  3. 在 hunk 内逐行推进
     for each body line (非 header):
       - 以 "+" 或 " " (上下文行) 开头 → 参与 new-side 计数
         如果 newLine == absoluteLine → return newLine
         然后 newLine++
       - 以 "-" 开头 → 跳过（仅 old-side）

  4. 遍历完所有 hunk 找不到 → return -1

特殊情况:
  - diffText 为空或 null → return -1
  - 文件中无 hunk（纯新增/纯二进制等） → return -1
```

### 示例

```
diff --git a/Foo.java b/Foo.java
@@ -10,5 +10,7 @@

  unchanged line    → newLine=10, 不匹配
  unchanged line    → newLine=11, 不匹配
+ added line        → newLine=12, 匹配! → return 12
+ added line        → (不会走到)
  unchanged line

@@ -30,3 +35,4 @@
  ...
```

---

## H2 CLOB 兼容性

H2 的 `CLOB` 上限 2GB，读写 API 与 `VARCHAR` 一致（`PreparedStatement.setString` / `ResultSet.getString`）。无需引入 `java.sql.Clob`。

---

## `ReviewExecutorImpl` diff 采集

在 `cloneOrFetch()` 返回后、`buildCommand()` 之前：

```java
// 采集 PR diff 文本（供行号映射用）
Path diffFile = workdir.resolve("codeguard_diff.txt");
runCmd(workdir, 1, TimeUnit.MINUTES,
    "git", "diff", "origin/" + job.getBaseRef() + "..." + job.getHeadSha());
// 实际用 runCmd 的 stderr 重定向 → stdout
String diffText = runGitDiff(workdir, job);
job.setDiffText(diffText);
```

注意：不能用 `runCmd` 的通用方法（redirectErrorStream 导致错误信息混入）。需新增专门方法捕获 git diff 的 stdout：

```java
private String runGitDiff(Path workdir, ReviewJob job) throws IOException, InterruptedException {
    ProcessBuilder pb = new ProcessBuilder(
        "git", "diff", "origin/" + job.getBaseRef() + "..." + job.getHeadSha());
    pb.directory(workdir.toFile());
    pb.redirectErrorStream(false);
    Process p = pb.start();
    String stdout = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
    p.waitFor(1, TimeUnit.MINUTES);
    return stdout;
}
```

---

## `ResultFeedback` 改动

在 `postHighSeverityComments` 中：

```java
// 原有: Math.max(issue.path("line").asInt(), 1)
// 改为:
int absoluteLine = Math.max(issue.path("line").asInt(), 1);
int diffLine = mapToDiffLine(job.getDiffText(), issue.path("file").asText(), absoluteLine);
if (diffLine > 0) {
    boolean ok = client.createPRComment(..., diffLine, ...);
    // ...
}
// diffLine = -1 → 不进 createPRComment，自然被 failedIssues 收集 → 降级
```

已有降级逻辑（上一版修复的 `failedIssues` + `createIssueComment`）不删，`diffLine = -1` 的 issue 自动走降级路径。

---

## 错误处理

| 场景 | 处理 |
|------|------|
| git diff 失败（进程异常） | diffText 为空 → 所有 issue diffLine=-1 → 全部降级为普通评论 |
| diff 文本超长（>10MB） | CLOB 自动容纳；超时由 waitFor(1min) 控制 |
| 文件在 diff 中不存在（重命名/新文件） | 分隔符匹配不到 → 返回 -1 → 降级 |
| hunk header 格式异常 | 正则匹配失败 → 跳过该 hunk |

降级兜底保证：最坏情况下所有 CRITICAL issue 都会以 PR 普通评论形式出现，信息不丢。

---

## 测试策略

### Java 单测

| 测试 | 内容 |
|------|------|
| `mapToDiffLine_findsLineInHunk` | 正常 unified diff，源文件行号 12 映射到 diff 行号 12 |
| `mapToDiffLine_returnsNegativeWhenNotFound` | 行号 99 不在任何 hunk 内 → -1 |
| `mapToDiffLine_emptyDiff` | diffText=null/"" → -1 |
| `mapToDiffLine_multiHunk` | 多个 hunk，验证每个 hunk 内行号映射正确 |
| `mapToDiffLine_fileNotFound` | diff 中无该文件 → -1 |
| `mapToDiffLine_skipsDeletedLines` | "-" 行不参与 new-side 计数 |

### 集成验证

1. 部署 Gateway → 触发真实 PR 审查
2. 日志中 `PR 行级评论跳过(行号无法映射)` 出现次数应显著减少
3. 降级评论仍存在但仅覆盖"LLM 报了 diff 上下文外的行号"的场景

---

## 不实现的部分

- 邻近兜底（行号偏几行时用最近的 diff 行代替）
- diff 压缩存储

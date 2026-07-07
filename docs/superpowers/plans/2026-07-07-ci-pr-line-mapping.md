# CI PR 行级评论行号映射修复 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 LLM 报的绝对行号映射为 diff 内的行号，修复行级评论 422 失败。

**Architecture:** `ReviewExecutorImpl` 采集 `git diff` → 存入 `ReviewJob.diffText`（H2 CLOB）→ `ResultFeedback` 用 `mapToDiffLine()` 解析 unified diff 做行号映射 → 映射成功用 diff 行号发评论，失败走已有降级路径。

**Tech Stack:** Java 21 + H2 + JUnit 5

**Spec:** `docs/superpowers/specs/2026-07-07-ci-pr-line-mapping-design.md`

---

## 文件结构

### 新建

| 文件 | 职责 |
|------|------|
| `services/gateway/src/test/java/com/codeguard/ci/executor/ResultFeedbackLineMappingTest.java` | `mapToDiffLine` 6 个单测 |

### 修改

| 文件 | 改动 |
|------|------|
| `services/gateway/src/main/java/com/codeguard/ci/model/ReviewJob.java` | +`diffText` 字段 + getter/setter/dbSetter |
| `services/gateway/src/main/java/com/codeguard/ci/job/JobRepository.java` | DDL/DML/mapRow 加 `diff_text CLOB` 列 |
| `services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutorImpl.java` | clone 后 `git diff` → `job.setDiffText()` |
| `services/gateway/src/main/java/com/codeguard/ci/executor/ResultFeedback.java` | +`mapToDiffLine()` + 评论时使用映射后的行号 |

---

### Task 1: ReviewJob 加 diffText 字段

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/ci/model/ReviewJob.java`

- [ ] **Step 1: 加字段**

在 `ReviewJob` 类的字段区（`private long installationId` 附近）：

```java
private String diffText;
```

在 getter 区（`getInstallationId()` 附近）：

```java
public String getDiffText() { return diffText; }
```

在 setter 区（`setInstallationId()` 附近）：

```java
public void setDiffText(String diffText) { this.diffText = diffText; this.updatedAt = Instant.now(); }
```

在 dbSetter 区（`setInstallationIdFromDb()` 附近）：

```java
public void setDiffTextFromDb(String diffText) { this.diffText = diffText; }
```

- [ ] **Step 2: 编译验证**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway; mvn compile -q; if ($?) { "BUILD SUCCESS" } else { "BUILD FAILED" }
```

Expected: BUILD SUCCESS

- [ ] **Step 3: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/model/ReviewJob.java
git commit -m "feat(ci): ReviewJob 新增 diffText 字段，供行号映射使用"
```

---

### Task 2: JobRepository 加 diff_text 列

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/ci/job/JobRepository.java`

- [ ] **Step 1: DDL 加列**

在 `initTable()` 的 CREATE TABLE 中，`error_message VARCHAR(1024),` 之后加：

```sql
diff_text       CLOB,
```

- [ ] **Step 2: INSERT 加列**

在 `insert()` 的 MERGE INTO 列列表中加 `diff_text`：

```java
// 修改 MERGE INTO 的 column list（在 VALUES 之前）：
// 原: MERGE INTO review_jobs (repo, pr_number, head_sha, base_ref, clone_url, installation_id,
//                              status, result_json, retry_count, error_message, created_at, updated_at)
// 改为:
String sql = """
    MERGE INTO review_jobs (repo, pr_number, head_sha, base_ref, clone_url, installation_id,
                            status, result_json, retry_count, error_message, diff_text, created_at, updated_at)
    KEY (repo, pr_number, head_sha)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """;
```

对应 VALUES 中的 `ps.setXxx` 调用，在 `ps.setString(10, job.getErrorMessage());` 之后追加：

```java
ps.setString(11, job.getDiffText());
ps.setTimestamp(12, Timestamp.from(job.getCreatedAt()));
ps.setTimestamp(13, Timestamp.from(job.getUpdatedAt()));
```

- [ ] **Step 3: UPDATE 加列**

在 `update()` 的 SQL 中加 `diff_text`：

```java
// 原:
// UPDATE review_jobs SET status = ?, result_json = ?, retry_count = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
// 改为:
String sql = """
    UPDATE review_jobs
    SET status = ?, result_json = ?, retry_count = ?, error_message = ?, diff_text = ?, updated_at = CURRENT_TIMESTAMP
    WHERE id = ?
    """;
```

对应 `ps.setXxx`，在 `ps.setString(4, job.getErrorMessage());` 之后、`ps.setLong(5, job.getId());` 之前加：

```java
ps.setString(5, job.getDiffText());
```

然后把后面的 `ps.setLong(5, job.getId())` 改为 `ps.setLong(6, job.getId())`。

- [ ] **Step 4: mapRow 读列**

在 `mapRow()` 中，`job.setErrorMessageFromDb(rs.getString("error_message"));` 之后加：

```java
job.setDiffTextFromDb(rs.getString("diff_text"));
```

- [ ] **Step 5: 编译 + 全量单测验证**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures

- [ ] **Step 6: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/job/JobRepository.java
git commit -m "feat(ci): JobRepository 新增 diff_text CLOB 列——DDL/INSERT/UPDATE/mapRow"
```

---

### Task 3: ReviewExecutorImpl 采集 diff 存入 job

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutorImpl.java`

- [ ] **Step 1: 新增 runGitDiff 方法**

在 `ReviewExecutorImpl` 类中，`runCmd` 方法之后、`ProcessTimeoutException` 静态内部类之前，追加：

```java
private String runGitDiff(Path workdir, ReviewJob job) {
    try {
        ProcessBuilder pb = new ProcessBuilder(
            "git", "diff", "origin/" + job.getBaseRef() + "..." + job.getHeadSha());
        pb.directory(workdir.toFile());
        pb.redirectErrorStream(false);
        Process p = pb.start();
        String stdout = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        boolean finished = p.waitFor(1, TimeUnit.MINUTES);
        if (!finished) {
            p.destroyForcibly();
            log.warn("git diff 超时(1min): {}", job.dedupKey());
            return "";
        }
        if (p.exitValue() != 0) {
            String stderr = new String(p.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);
            log.warn("git diff 失败(exit={}): {} stderr={}", p.exitValue(), job.dedupKey(), stderr);
            return "";
        }
        return stdout;
    } catch (Exception e) {
        log.warn("git diff 异常: {} {}", job.dedupKey(), e.getMessage());
        return "";
    }
}
```

- [ ] **Step 2: 在 accept() 中调用 runGitDiff**

在 `accept()` 方法中，`workdir = cloneOrFetch(job);` 之后、`List<String> cmd = buildCommand(workdir, job);` 之前，插入：

```java
// 采集 diff 文本（供行号映射用）
job.setDiffText(runGitDiff(workdir, job));
```

- [ ] **Step 3: 编译 + 全量单测**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures

- [ ] **Step 4: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutorImpl.java
git commit -m "feat(ci): ReviewExecutorImpl 采集 git diff 并存入 ReviewJob"
```

---

### Task 4: ResultFeedback 行号映射 + 单测

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/ci/executor/ResultFeedback.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/executor/ResultFeedbackLineMappingTest.java`

- [ ] **Step 1: 写失败单测**

```java
package com.codeguard.ci.executor;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class ResultFeedbackLineMappingTest {

    @Test
    void findsLineInSimpleHunk() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            index abc..def 100644
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,5 +10,7 @@
              unchanged 1
              unchanged 2
            + added line
            + another added
              unchanged 3
            """;
        // 源文件第12行（第二个 +added）在 diff 中出现在 new-side 第12行
        int result = ResultFeedback.mapToDiffLine(diff, "Foo.java", 12);
        assertTrue(result > 0, "Should find line 12 in diff, got: " + result);
    }

    @Test
    void returnsNegativeWhenLineNotFound() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,3 +10,4 @@
              line10
              line11
            + added
            """;
        int result = ResultFeedback.mapToDiffLine(diff, "Foo.java", 99);
        assertEquals(-1, result);
    }

    @Test
    void emptyDiffReturnsNegative() {
        assertEquals(-1, ResultFeedback.mapToDiffLine(null, "Foo.java", 10));
        assertEquals(-1, ResultFeedback.mapToDiffLine("", "Foo.java", 10));
    }

    @Test
    void multiHunkMapping() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,5 +10,7 @@
              ctx10
              ctx11
            + added12
            + added13
              ctx14
            @@ -30,3 +35,4 @@
              ctx35
              ctx36
            + added37
            """;
        assertEquals(12, ResultFeedback.mapToDiffLine(diff, "Foo.java", 12));
        assertEquals(13, ResultFeedback.mapToDiffLine(diff, "Foo.java", 13));
        assertEquals(37, ResultFeedback.mapToDiffLine(diff, "Foo.java", 37));
    }

    @Test
    void fileNotFoundInDiff() {
        String diff = """
            diff --git a/Bar.java b/Bar.java
            --- a/Bar.java
            +++ b/Bar.java
            @@ -1,1 +1,2 @@
            + new
            """;
        assertEquals(-1, ResultFeedback.mapToDiffLine(diff, "Foo.java", 10));
    }

    @Test
    void skipsDeletedLines() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,5 +10,4 @@
              ctx10
            - deleted11
              ctx12
            + added13
              ctx14
            """;
        // deleted11 不参与 new-side 计数，所以 ctx12 仍然是 new-side 12
        assertEquals(12, ResultFeedback.mapToDiffLine(diff, "Foo.java", 12));
        assertEquals(13, ResultFeedback.mapToDiffLine(diff, "Foo.java", 13));
    }
}
```

Run to confirm failure:
```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=ResultFeedbackLineMappingTest -DfailIfNoTests=false 2>&1 | Select-String "Tests run|FAIL|BUILD"
```

Expected: BUILD FAILURE（方法不存在）

- [ ] **Step 2: 实现 mapToDiffLine**

在 `ResultFeedback.java` 中，`postHighSeverityComments` 方法之前，新增静态方法：

```java
/**
 * 将源文件绝对行号映射为 unified diff 中的 new-side 行号。
 * <p>
 * 解析 diff 文本，找到目标文件对应的 hunk，在 hunk 内逐行推进 new-side 行号计数，
 * 匹配到 absoluteLine 时返回对应的 new-side 行号。
 *
 * @param diffText     unified diff 全文
 * @param targetFile   目标文件路径（如 "src/main/java/com/example/Foo.java"）
 * @param absoluteLine 源文件中的绝对行号
 * @return diff 内的 new-side 行号；找不到返回 -1
 */
static int mapToDiffLine(String diffText, String targetFile, int absoluteLine) {
    if (diffText == null || diffText.isEmpty() || targetFile == null || absoluteLine <= 0) {
        return -1;
    }

    // 按文件拆分：找 "diff --git a/" + targetFile 开头的块
    String fileMarker = "diff --git a/" + targetFile;
    int fileStart = diffText.indexOf(fileMarker);
    if (fileStart == -1) {
        // 也尝试 b/ 前缀（某些 diff 格式差异）
        fileMarker = "diff --git b/" + targetFile;
        fileStart = diffText.indexOf(fileMarker);
        if (fileStart == -1) return -1;
    }

    // 找到下一个文件的起始或文本末尾作为本文件的结束
    int nextFileStart = diffText.indexOf("diff --git ", fileStart + fileMarker.length());
    String fileBlock = nextFileStart == -1
        ? diffText.substring(fileStart)
        : diffText.substring(fileStart, nextFileStart);

    // 逐 hunk 解析
    java.util.regex.Pattern hunkPattern = java.util.regex.Pattern.compile(
        "@@ -(\\d+)(?:,\\d+)? \\+(\\d+)(?:,\\d+)? @@");
    java.util.regex.Matcher m = hunkPattern.matcher(fileBlock);

    while (m.find()) {
        int newLine = Integer.parseInt(m.group(2));
        // 找到这个 hunk 的 body 起始（hunk header 行的下一行）
        int bodyStart = m.end();
        int bodyEnd = fileBlock.indexOf("@@", bodyStart);
        if (bodyEnd == -1) bodyEnd = fileBlock.length();
        String body = fileBlock.substring(bodyStart, bodyEnd);

        for (String line : body.split("\n")) {
            if (line.startsWith("-")) {
                // 删除行不参与 new-side 计数
                continue;
            }
            if (line.startsWith("+") || line.startsWith(" ")) {
                // 新增行或上下文行参与 new-side 计数
                if (newLine == absoluteLine) {
                    return newLine;
                }
                newLine++;
            }
            // 其他行（如 "\ No newline at end of file"）跳过
        }
    }
    return -1;
}
```

- [ ] **Step 3: 跑单测确认通过**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=ResultFeedbackLineMappingTest 2>&1 | Select-String "Tests run|BUILD"
```

Expected: Tests run: 6, Failures: 0, BUILD SUCCESS

- [ ] **Step 4: 在 postHighSeverityComments 中使用映射行号**

修改 `postHighSeverityComments` 方法中创建评论的行号逻辑。找到：

```java
boolean ok = client.createPRComment(job.getRepo(), job.getPrNumber(),
    job.getHeadSha(), issue.path("file").asText(),
    Math.max(issue.path("line").asInt(), 1),
    body, job.getInstallationId());
```

改为：

```java
int absoluteLine = Math.max(issue.path("line").asInt(), 1);
int diffLine = mapToDiffLine(job.getDiffText(), issue.path("file").asText(), absoluteLine);
if (diffLine > 0) {
    boolean ok = client.createPRComment(job.getRepo(), job.getPrNumber(),
        job.getHeadSha(), issue.path("file").asText(),
        diffLine,
        body, job.getInstallationId());
    if (!ok) {
        failedIssues.add(String.format("- `%s:%d` **%s**: %s",
            issue.path("file").asText(), absoluteLine,
            issue.path("type").asText(), issue.path("message").asText()));
    }
} else {
    // diffLine = -1: 行号不在 diff 上下文内，降级
    failedIssues.add(String.format("- `%s:%d` **%s**: %s",
        issue.path("file").asText(), absoluteLine,
        issue.path("type").asText(), issue.path("message").asText()));
}
```

- [ ] **Step 5: 跑全量单测**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures

- [ ] **Step 6: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/executor/ResultFeedback.java
git add services/gateway/src/test/java/com/codeguard/ci/executor/ResultFeedbackLineMappingTest.java
git commit -m "feat(ci): ResultFeedback 新增 mapToDiffLine——绝对行号→diff 行号映射"
```

---

### Task 5: ADR 写入 + 最终验证

**Files:**
- Modify: `DECISIONS.md`

- [ ] **Step 1: 跑 Java 全量单测**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures

- [ ] **Step 2: 写 ADR**

读 `DECISIONS.md` 找到最后的 ADR 编号，追加：

```markdown
---

## ADR-037: CI PR 行级评论行号映射

**日期**：2026-07-07
**状态**：已实现

### 背景

LLM 审查输出的 `line` 是源文件绝对行号，GitHub PR review comment API 要求行号落在 diff hunk 的上下文行内。不匹配时返回 HTTP 422 "pull_request_review_thread.line could not be resolved"。之前只做了降级兜底（失败的 issue 汇总为 PR 普通评论），丢失了精确的行级定位。

### 决策

- **持久化 diff 文本**: `ReviewJob` 新增 `diffText` 字段，H2 `CLOB` 列。`ReviewExecutorImpl` 在 clone 后采集 `git diff base...HEAD`。
- **严格映射**: `ResultFeedback.mapToDiffLine()` 解析 unified diff hunk header，在 hunk 内逐行推进 new-side 行号计数。匹配返回 diff 行号，未匹配返回 -1 走降级路径。
- **降级保留**: 行号无法映射的 issue 仍以 PR 普通评论形式出现，信息不丢。

### 放弃的方案

- 邻近兜底（行号偏几行时用最近的 diff 行代替）—— 可能标错位置
- diff 压缩存储 —— 增加复杂度，CLOB 已足够
- 不持久化直接传参 —— 进程挂掉丢失

### 影响

- 4 个 Java 文件改动（ReviewJob / JobRepository / ReviewExecutorImpl / ResultFeedback）
- 1 个新单测文件（6 条）
- 预期：422 行号映射失败率大幅降低，仅剩 LLM 报了 diff 上下文外行号的边缘 case
```

- [ ] **Step 3: Commit**

```bash
git add DECISIONS.md
git commit -m "docs: ADR-037 CI PR 行级评论行号映射决策记录"
git push
```

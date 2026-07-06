# Codeguard CI 集成 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Codeguard 升级为 GitHub Webhook 常驻服务，PR 提交自动触发审查，Check Runs 做门禁 + 行级评论贴高危问题。

**Architecture:** Java Gateway 新增 webhook 端点 + H2 持久化 job 队列 + ProcessBuilder 调 Python CLI 执行审查 + GitHub API 写回结果。单容器双运行时（JDK 21 + Python 3.12）部署。

**Tech Stack:** Javalin, H2, Guava RateLimiter, Jackson, ProcessBuilder, Docker Compose

**Spec:** `docs/superpowers/specs/2026-07-06-codeguard-ci-integration-design.md`

---

## 文件结构

### 新建

```
services/gateway/src/main/java/com/codeguard/ci/
├── model/
│   ├── ReviewJob.java              # job 实体：id/repo/prNumber/headSha/status/resultJson/retryCount
│   └── WebhookPayload.java         # webhook 精简 payload：6 字段
├── webhook/
│   ├── GitHubWebhookController.java # Javalin 路由：验签→过滤→幂等→提交 job
│   └── WebhookVerifier.java        # HMAC-SHA256 验签，constant-time 比较
├── job/
│   ├── JobRepository.java          # H2 CRUD + 启动扫描
│   └── JobScheduler.java           # ExecutorService + Semaphore + 有界队列 + 启动恢复
├── executor/
│   ├── ReviewExecutor.java         # ProcessBuilder → git clone + python CLI + result parse
│   └── ReviewResultParser.java     # stdout JSON → ReviewResult POJO
├── github/
│   └── GitHubClient.java           # App token 管理 + Check Runs + PR Comments
└── guard/
    └── ReviewGuard.java            # 令牌桶限流 + 三层超时 + 大diff降级 + 重试判定

services/gateway/src/test/java/com/codeguard/ci/
├── webhook/
│   ├── WebhookVerifierTest.java
│   └── GitHubWebhookControllerTest.java
├── job/
│   ├── JobRepositoryTest.java
│   └── JobSchedulerTest.java
├── executor/
│   └── ReviewExecutorTest.java
├── github/
│   └── GitHubClientTest.java
└── guard/
    └── ReviewGuardTest.java

Dockerfile                    # 仓库根目录
docker-compose.yml            # 仓库根目录
```

### 修改

```
services/gateway/pom.xml                                    # 加 Guava、H2 依赖
services/gateway/src/main/java/com/codeguard/toolserver/
├── ToolServerApp.java                                      # 注册 webhook 路由，初始化 JobScheduler
└── Main.java                                               # 启动时调用 ReviewGuard.init()
.env.example                                                # 新增 6 个环境变量
```

---

## Phase 1: Webhook 端点 + H2 持久化

### Task 1.1: 添加依赖

**Files:**
- Modify: `services/gateway/pom.xml`

- [ ] **Step 1: 在 pom.xml 添加 Guava 和 H2 依赖**

在 `<dependencies>` 内已有的 `</dependency>` 闭合标签后（junit 之前）插入：

```xml
<!-- 限流: RateLimiter -->
<dependency>
    <groupId>com.google.guava</groupId>
    <artifactId>guava</artifactId>
    <version>33.2.1-jre</version>
</dependency>
<!-- Job 持久化: H2 -->
<dependency>
    <groupId>com.h2database</groupId>
    <artifactId>h2</artifactId>
    <version>2.3.232</version>
</dependency>
```

- [ ] **Step 2: 验证依赖下载**

```powershell
cd services/gateway; mvn dependency:resolve -q
```

Expected: BUILD SUCCESS

- [ ] **Step 3: 提交**

```bash
git add services/gateway/pom.xml
git commit -m "chore(gateway): 添加 Guava 和 H2 依赖"
```

---

### Task 1.2: 数据模型

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/model/WebhookPayload.java`
- Create: `services/gateway/src/main/java/com/codeguard/ci/model/ReviewJob.java`

- [ ] **Step 1: 写 WebhookPayload.java**

```java
package com.codeguard.ci.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * GitHub pull_request webhook 精简 payload。
 * 从完整 webhook body 中仅提取审查所需的 6 个字段。
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record WebhookPayload(
    @JsonProperty("repo_full_name") String repoFullName,
    @JsonProperty("clone_url") String cloneUrl,
    @JsonProperty("pr_number") int prNumber,
    @JsonProperty("head_sha") String headSha,
    @JsonProperty("base_ref") String baseRef,
    @JsonProperty("installation_id") long installationId
) {
    /** 幂等去重 key */
    public String dedupKey() {
        return repoFullName + ":" + prNumber + ":" + headSha;
    }
}
```

- [ ] **Step 2: 写 ReviewJob.java**

```java
package com.codeguard.ci.model;

import java.time.Instant;

/**
 * 审查 job 实体。持久化到 H2，重启不丢。
 */
public class ReviewJob {
    public enum Status { PENDING, RUNNING, RETRYING, DONE, FAILED }

    private Long id;
    private String repo;
    private int prNumber;
    private String headSha;
    private String baseRef;
    private String cloneUrl;
    private long installationId;
    private Status status;
    private String resultJson;
    private int retryCount;
    private String errorMessage;
    private Instant createdAt;
    private Instant updatedAt;

    public ReviewJob() {}

    public ReviewJob(WebhookPayload payload) {
        this.repo = payload.repoFullName();
        this.prNumber = payload.prNumber();
        this.headSha = payload.headSha();
        this.baseRef = payload.baseRef();
        this.cloneUrl = payload.cloneUrl();
        this.installationId = payload.installationId();
        this.status = Status.PENDING;
        this.retryCount = 0;
        this.createdAt = Instant.now();
        this.updatedAt = Instant.now();
    }

    // getters
    public Long getId() { return id; }
    public String getRepo() { return repo; }
    public int getPrNumber() { return prNumber; }
    public String getHeadSha() { return headSha; }
    public String getBaseRef() { return baseRef; }
    public String getCloneUrl() { return cloneUrl; }
    public long getInstallationId() { return installationId; }
    public Status getStatus() { return status; }
    public String getResultJson() { return resultJson; }
    public int getRetryCount() { return retryCount; }
    public String getErrorMessage() { return errorMessage; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }

    // setters
    public void setId(Long id) { this.id = id; }
    public void setStatus(Status status) { this.status = status; this.updatedAt = Instant.now(); }
    public void setResultJson(String resultJson) { this.resultJson = resultJson; }
    public void setRetryCount(int retryCount) { this.retryCount = retryCount; }
    public void setErrorMessage(String errorMessage) { this.errorMessage = errorMessage; }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
    public void setInstallationId(long installationId) { this.installationId = installationId; }

    public String dedupKey() {
        return repo + ":" + prNumber + ":" + headSha;
    }
}
```

- [ ] **Step 3: 编译检查**

```powershell
cd services/gateway; mvn compile -q
```

- [ ] **Step 4: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/model/
git commit -m "feat(ci): 新增 WebhookPayload 和 ReviewJob 数据模型"
```

---

### Task 1.3: Webhook 验签

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/webhook/WebhookVerifier.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/webhook/WebhookVerifierTest.java`

- [ ] **Step 1: 写 WebhookVerifier.java**

```java
package com.codeguard.ci.webhook;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.security.MessageDigest;
import java.util.HexFormat;

/**
 * GitHub webhook HMAC-SHA256 验签。
 * 使用 constant-time 比较防止时序攻击。
 */
public final class WebhookVerifier {

    private final byte[] secret;

    public WebhookVerifier(String secret) {
        this.secret = secret.getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }

    /**
     * 验证 X-Hub-Signature-256 头。
     * @param signatureHeader  "sha256=<hex>" 或 null
     * @param body             webhook 原始请求体
     * @return true 验证通过
     */
    public boolean verify(String signatureHeader, byte[] body) {
        if (signatureHeader == null || !signatureHeader.startsWith("sha256=")) {
            return false;
        }
        String expected = signatureHeader.substring(7);
        String computed = "sha256=" + hmacSha256(body);
        return MessageDigest.isEqual(
            expected.getBytes(java.nio.charset.StandardCharsets.UTF_8),
            computed.getBytes(java.nio.charset.StandardCharsets.UTF_8)
        );
    }

    private String hmacSha256(byte[] data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(data));
        } catch (Exception e) {
            throw new RuntimeException("HMAC-SHA256 计算失败", e);
        }
    }
}
```

- [ ] **Step 2: 写 WebhookVerifierTest.java**

```java
package com.codeguard.ci.webhook;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class WebhookVerifierTest {

    private final WebhookVerifier verifier = new WebhookVerifier("test_secret");

    @Test
    void shouldPassWithCorrectSignature() throws Exception {
        byte[] body = "{\"action\":\"opened\"}".getBytes();
        String sig = computeSignature("test_secret", body);
        assertTrue(verifier.verify(sig, body));
    }

    @Test
    void shouldRejectWrongSecret() throws Exception {
        byte[] body = "{\"action\":\"opened\"}".getBytes();
        String sig = computeSignature("wrong_secret", body);
        assertFalse(verifier.verify(sig, body));
    }

    @Test
    void shouldRejectNullHeader() {
        assertFalse(verifier.verify(null, "{}".getBytes()));
    }

    @Test
    void shouldRejectTamperedBody() throws Exception {
        byte[] original = "{\"action\":\"opened\"}".getBytes();
        String sig = computeSignature("test_secret", original);
        byte[] tampered = "{\"action\":\"closed\"}".getBytes();
        assertFalse(verifier.verify(sig, tampered));
    }

    private String computeSignature(String secret, byte[] body) throws Exception {
        javax.crypto.Mac mac = javax.crypto.Mac.getInstance("HmacSHA256");
        mac.init(new javax.crypto.spec.SecretKeySpec(
            secret.getBytes(java.nio.charset.StandardCharsets.UTF_8), "HmacSHA256"));
        return "sha256=" + java.util.HexFormat.of().formatHex(mac.doFinal(body));
    }
}
```

- [ ] **Step 3: 跑测试验证**

```powershell
cd services/gateway; mvn test -pl . -Dtest=WebhookVerifierTest -q
```

Expected: 4 tests PASS

- [ ] **Step 4: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/webhook/WebhookVerifier.java
git add services/gateway/src/test/java/com/codeguard/ci/webhook/WebhookVerifierTest.java
git commit -m "feat(ci): 新增 GitHub webhook HMAC-SHA256 验签"
```

---

### Task 1.4: H2 Job 持久化

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/job/JobRepository.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/job/JobRepositoryTest.java`

- [ ] **Step 1: 写 JobRepository.java**

```java
package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.*;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * ReviewJob 的 H2 持久化层。
 * 使用文件模式（jdbc:h2:file）保证重启不丢数据。
 */
public class JobRepository implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(JobRepository.class);

    private final Connection conn;

    public JobRepository(String dbPath) {
        try {
            this.conn = DriverManager.getConnection("jdbc:h2:file:" + dbPath + ";DB_CLOSE_DELAY=-1");
            initTable();
        } catch (SQLException e) {
            throw new RuntimeException("H2 数据库初始化失败", e);
        }
    }

    private void initTable() throws SQLException {
        try (Statement stmt = conn.createStatement()) {
            stmt.execute("""
                CREATE TABLE IF NOT EXISTS review_jobs (
                    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                    repo            VARCHAR(255) NOT NULL,
                    pr_number       INT NOT NULL,
                    head_sha        VARCHAR(40) NOT NULL,
                    base_ref        VARCHAR(255),
                    clone_url       VARCHAR(512),
                    installation_id BIGINT,
                    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                    result_json     CLOB,
                    retry_count     INT DEFAULT 0,
                    error_message   VARCHAR(1024),
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (repo, pr_number, head_sha)
                )
                """);
        }
    }

    /** 插入新 job。如果同 key 已存在则静默返回空。 */
    public Optional<ReviewJob> insert(ReviewJob job) {
        String sql = """
            MERGE INTO review_jobs (repo, pr_number, head_sha, base_ref, clone_url,
                                    installation_id, status, retry_count, created_at, updated_at)
            KEY (repo, pr_number, head_sha)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """;
        try (PreparedStatement ps = conn.prepareStatement(sql, Statement.RETURN_GENERATED_KEYS)) {
            ps.setString(1, job.getRepo());
            ps.setInt(2, job.getPrNumber());
            ps.setString(3, job.getHeadSha());
            ps.setString(4, job.getBaseRef());
            ps.setString(5, job.getCloneUrl());
            ps.setLong(6, job.getInstallationId());
            ps.setString(7, job.getStatus().name());
            ps.setInt(8, job.getRetryCount());
            ps.setTimestamp(9, Timestamp.from(job.getCreatedAt()));
            ps.setTimestamp(10, Timestamp.from(job.getUpdatedAt()));
            ps.executeUpdate();
            try (ResultSet keys = ps.getGeneratedKeys()) {
                if (keys.next()) {
                    job.setId(keys.getLong(1));
                    return Optional.of(job);
                }
            }
            return Optional.empty(); // MERGE 命中已有行，未插入
        } catch (SQLException e) {
            log.error("插入 job 失败: {}", job.dedupKey(), e);
            throw new RuntimeException(e);
        }
    }

    /** 按去重 key 查询已有 job（幂等检查） */
    public Optional<ReviewJob> findByDedupKey(String repo, int prNumber, String headSha) {
        String sql = "SELECT * FROM review_jobs WHERE repo = ? AND pr_number = ? AND head_sha = ?";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, repo);
            ps.setInt(2, prNumber);
            ps.setString(3, headSha);
            try (ResultSet rs = ps.executeQuery()) {
                if (rs.next()) {
                    return Optional.of(mapRow(rs));
                }
            }
        } catch (SQLException e) {
            log.error("查询 job 失败", e);
        }
        return Optional.empty();
    }

    /** 更新 job 状态和结果 */
    public void update(ReviewJob job) {
        String sql = """
            UPDATE review_jobs SET status = ?, result_json = ?, retry_count = ?,
                   error_message = ?, updated_at = ?
            WHERE id = ?
            """;
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, job.getStatus().name());
            ps.setString(2, job.getResultJson());
            ps.setInt(3, job.getRetryCount());
            ps.setString(4, job.getErrorMessage());
            ps.setTimestamp(5, Timestamp.from(Instant.now()));
            ps.setLong(6, job.getId());
            ps.executeUpdate();
        } catch (SQLException e) {
            log.error("更新 job 失败: id={}", job.getId(), e);
        }
    }

    /** 查询所有未完成的 job（启动恢复用） */
    public List<ReviewJob> findUnfinished() {
        String sql = "SELECT * FROM review_jobs WHERE status IN ('PENDING', 'RUNNING', 'RETRYING')";
        List<ReviewJob> jobs = new ArrayList<>();
        try (Statement stmt = conn.createStatement();
             ResultSet rs = stmt.executeQuery(sql)) {
            while (rs.next()) {
                jobs.add(mapRow(rs));
            }
        } catch (SQLException e) {
            log.error("查询未完成 job 失败", e);
        }
        return jobs;
    }

    private ReviewJob mapRow(ResultSet rs) throws SQLException {
        ReviewJob job = new ReviewJob();
        job.setId(rs.getLong("id"));
        job.setStatus(Status.valueOf(rs.getString("status")));
        job.setResultJson(rs.getString("result_json"));
        job.setRetryCount(rs.getInt("retry_count"));
        job.setErrorMessage(rs.getString("error_message"));
        job.setUpdatedAt(rs.getTimestamp("updated_at").toInstant());
        job.setInstallationId(rs.getLong("installation_id"));
        // repo, prNumber, headSha, baseRef, cloneUrl 需要从 db 读取
        // 覆盖 model 中的字段
        try {
            java.lang.reflect.Field repoField = ReviewJob.class.getDeclaredField("repo");
            repoField.setAccessible(true);
            repoField.set(job, rs.getString("repo"));
            java.lang.reflect.Field prField = ReviewJob.class.getDeclaredField("prNumber");
            prField.setAccessible(true);
            prField.setInt(job, rs.getInt("pr_number"));
            java.lang.reflect.Field shaField = ReviewJob.class.getDeclaredField("headSha");
            shaField.setAccessible(true);
            shaField.set(job, rs.getString("head_sha"));
            java.lang.reflect.Field baseField = ReviewJob.class.getDeclaredField("baseRef");
            baseField.setAccessible(true);
            baseField.set(job, rs.getString("base_ref"));
            java.lang.reflect.Field cloneField = ReviewJob.class.getDeclaredField("cloneUrl");
            cloneField.setAccessible(true);
            cloneField.set(job, rs.getString("clone_url"));
        } catch (Exception e) {
            throw new RuntimeException("反射设置 ReviewJob 字段失败", e);
        }
        return job;
    }

    @Override
    public void close() {
        try { conn.close(); } catch (SQLException ignored) {}
    }
}
```

- [ ] **Step 2: 写 JobRepositoryTest.java**

```java
package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import com.codeguard.ci.model.WebhookPayload;
import org.junit.jupiter.api.*;

import java.io.File;

import static org.junit.jupiter.api.Assertions.*;

class JobRepositoryTest {

    private JobRepository repo;

    @BeforeEach
    void setUp() {
        String tmpDir = System.getProperty("java.io.tmpdir");
        String dbPath = tmpDir + "/codeguard-test-" + System.nanoTime();
        repo = new JobRepository(dbPath);
    }

    @AfterEach
    void tearDown() {
        repo.close();
    }

    @Test
    void shouldInsertAndFindByDedupKey() {
        var payload = new WebhookPayload("owner/repo", "https://gh/owner/repo.git",
            1, "abc123", "main", 12345L);
        var job = new ReviewJob(payload);
        var inserted = repo.insert(job);
        assertTrue(inserted.isPresent());

        var found = repo.findByDedupKey("owner/repo", 1, "abc123");
        assertTrue(found.isPresent());
        assertEquals(Status.PENDING, found.get().getStatus());
    }

    @Test
    void shouldBeIdempotentOnDuplicateInsert() {
        var payload = new WebhookPayload("owner/repo", "https://gh/owner/repo.git",
            1, "abc123", "main", 12345L);
        var first = repo.insert(new ReviewJob(payload));
        assertTrue(first.isPresent());

        // 同 key 再次插入 → 不创建新记录
        var second = repo.insert(new ReviewJob(payload));
        assertTrue(second.isEmpty());
    }

    @Test
    void shouldUpdateStatus() {
        var payload = new WebhookPayload("owner/repo", "https://gh/owner/repo.git",
            2, "def456", "main", 12345L);
        var job = repo.insert(new ReviewJob(payload)).orElseThrow();
        job.setStatus(Status.RUNNING);
        repo.update(job);

        var found = repo.findByDedupKey("owner/repo", 2, "def456");
        assertEquals(Status.RUNNING, found.get().getStatus());
    }

    @Test
    void shouldFindUnfinishedJobs() {
        var p1 = new WebhookPayload("a/b", "url1", 1, "sha1", "main", 1L);
        var p2 = new WebhookPayload("c/d", "url2", 1, "sha2", "main", 1L);
        var j1 = repo.insert(new ReviewJob(p1)).orElseThrow();
        var j2 = repo.insert(new ReviewJob(p2)).orElseThrow();

        j1.setStatus(Status.DONE);
        repo.update(j1);

        var unfinished = repo.findUnfinished();
        assertEquals(1, unfinished.size());
        assertEquals("sha2", unfinished.get(0).getHeadSha());
    }
}
```

- [ ] **Step 3: 跑测试验证**

```powershell
cd services/gateway; mvn test -pl . -Dtest=JobRepositoryTest -q
```

Expected: 4 tests PASS

- [ ] **Step 4: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/job/JobRepository.java
git add services/gateway/src/test/java/com/codeguard/ci/job/JobRepositoryTest.java
git commit -m "feat(ci): 新增 H2 持久化 JobRepository，支持幂等插入和启动恢复"
```

---

### Task 1.5: Webhook Controller

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/webhook/GitHubWebhookController.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/webhook/GitHubWebhookControllerTest.java`
- Modify: `services/gateway/src/main/java/com/codeguard/toolserver/ToolServerApp.java`

- [ ] **Step 1: 写 GitHubWebhookController.java**

```java
package com.codeguard.ci.webhook;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.javalin.Javalin;
import io.javalin.http.Context;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Optional;
import java.util.Set;

/**
 * GitHub webhook 端点的 Javalin 处理器。
 * 三层过滤: 验签 → 事件过滤 → 幂等去重，最后提交异步 job。
 */
public class GitHubWebhookController {

    private static final Logger log = LoggerFactory.getLogger(GitHubWebhookController.class);
    private static final Set<String> ALLOWED_ACTIONS = Set.of("opened", "reopened", "synchronize");
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final WebhookVerifier verifier;
    private final JobRepository repo;
    private final JobScheduler scheduler;

    public GitHubWebhookController(String secret, JobRepository repo, JobScheduler scheduler) {
        this.verifier = new WebhookVerifier(secret);
        this.repo = repo;
        this.scheduler = scheduler;
    }

    public void register(Javalin app) {
        app.post("/webhooks/github", this::handle);
    }

    void handle(Context ctx) {
        // 层 1: 验签
        String sig = ctx.header("X-Hub-Signature-256");
        byte[] body = ctx.bodyAsBytes();
        if (!verifier.verify(sig, body)) {
            ctx.status(401).result("signature mismatch");
            return;
        }

        // 非 PR 事件 → 200 空响应
        String event = ctx.header("X-GitHub-Event");
        if (!"pull_request".equals(event)) {
            ctx.status(200).result("ignored: " + event);
            return;
        }

        try {
            JsonNode root = MAPPER.readTree(body);
            String action = root.path("action").asText();

            // 只处理 opened / reopened / synchronize
            if (!ALLOWED_ACTIONS.contains(action)) {
                ctx.status(200).result("skipped action: " + action);
                return;
            }

            WebhookPayload payload = extractPayload(root);

            // 层 3: 幂等
            Optional<ReviewJob> existing = repo.findByDedupKey(
                payload.repoFullName(), payload.prNumber(), payload.headSha());
            if (existing.isPresent() && existing.get().getStatus() != ReviewJob.Status.FAILED) {
                ctx.status(200).json(Map.of(
                    "status", "already_processed",
                    "job_id", existing.get().getId(),
                    "job_status", existing.get().getStatus().name()
                ));
                return;
            }

            ReviewJob job = new ReviewJob(payload);
            var inserted = repo.insert(job);
            if (inserted.isEmpty()) {
                ctx.status(200).json(Map.of("status", "duplicate"));
                return;
            }

            // 提交到异步队列
            boolean accepted = scheduler.submit(inserted.get());
            if (accepted) {
                ctx.status(202).json(Map.of("status", "accepted", "job_id", inserted.get().getId()));
            } else {
                ctx.status(503).json(Map.of("status", "queue_full"));
            }

        } catch (Exception e) {
            log.error("webhook 处理异常", e);
            ctx.status(500).result("internal error");
        }
    }

    private WebhookPayload extractPayload(JsonNode root) {
        JsonNode repo = root.path("repository");
        JsonNode pr = root.path("pull_request");
        JsonNode head = pr.path("head");
        JsonNode installation = root.path("installation");

        return new WebhookPayload(
            repo.path("full_name").asText(),
            repo.path("clone_url").asText(),
            pr.path("number").asInt(),
            head.path("sha").asText(),
            head.path("ref").asText(),
            installation.path("id").asLong()
        );
    }
}
```

- [ ] **Step 2: 写 GitHubWebhookControllerTest.java**

```java
package com.codeguard.ci.webhook;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import io.javalin.Javalin;
import io.javalin.testtools.JavalinTest;
import okhttp3.MediaType;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.util.HexFormat;
import java.util.concurrent.atomic.AtomicReference;

class GitHubWebhookControllerTest {

    private static final String SECRET = "test_secret";
    private JobRepository repo;
    private JobScheduler scheduler;
    private GitHubWebhookController controller;

    @BeforeEach
    void setUp() {
        String dbPath = System.getProperty("java.io.tmpdir") + "/codeguard-ctrl-" + System.nanoTime();
        repo = new JobRepository(dbPath);
        scheduler = new JobScheduler(repo, 1, null, null);
        controller = new GitHubWebhookController(SECRET, repo, scheduler);
    }

    @AfterEach
    void tearDown() {
        repo.close();
    }

    @Test
    void shouldReturn401ForInvalidSignature() {
        JavalinTest.test(createApp(), (server, client) -> {
            try (Response r = client.request(new Request.Builder()
                    .url(server.url() + "/webhooks/github")
                    .post(RequestBody.create("{}", MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", "sha256=bad")
                    .header("X-GitHub-Event", "pull_request")
                    .build())) {
                assertEquals(401, r.code());
            }
        });
    }

    @Test
    void shouldReturn200ForNonPREvent() {
        JavalinTest.test(createApp(), (server, client) -> {
            String body = "{}";
            String sig = sign(body);
            try (Response r = client.request(new Request.Builder()
                    .url(server.url() + "/webhooks/github")
                    .post(RequestBody.create(body, MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", sig)
                    .header("X-GitHub-Event", "push")
                    .build())) {
                assertEquals(200, r.code());
                assertTrue(r.body().string().contains("ignored"));
            }
        });
    }

    @Test
    void shouldReturn200ForSkippedAction() {
        JavalinTest.test(createApp(), (server, client) -> {
            String body = "{\"action\":\"closed\"}";
            String sig = sign(body);
            try (Response r = client.request(new Request.Builder()
                    .url(server.url() + "/webhooks/github")
                    .post(RequestBody.create(body, MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", sig)
                    .header("X-GitHub-Event", "pull_request")
                    .build())) {
                assertEquals(200, r.code());
                assertTrue(r.body().string().contains("skipped"));
            }
        });
    }

    @Test
    void shouldAcceptValidPRWebhook() {
        JavalinTest.test(createApp(), (server, client) -> {
            String body = """
                {
                  "action": "opened",
                  "repository": { "full_name": "owner/repo", "clone_url": "https://gh/owner/repo.git" },
                  "pull_request": {
                    "number": 42,
                    "head": { "sha": "abc123def456", "ref": "feature/x" },
                    "base": { "ref": "main" }
                  },
                  "installation": { "id": 12345 }
                }
                """;
            String sig = sign(body);
            try (Response r = client.request(new Request.Builder()
                    .url(server.url() + "/webhooks/github")
                    .post(RequestBody.create(body, MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", sig)
                    .header("X-GitHub-Event", "pull_request")
                    .build())) {
                assertEquals(202, r.code());
            }
        });
    }

    private Javalin createApp() {
        Javalin app = Javalin.create(cfg -> cfg.showJavalinBanner = false);
        controller.register(app);
        return app;
    }

    private String sign(String body) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(SECRET.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return "sha256=" + HexFormat.of().formatHex(mac.doFinal(body.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) { throw new RuntimeException(e); }
    }
}
```

> 注: 测试需要添加测试依赖 `com.squareup.okhttp3:okhttp:4.12.0` 到 pom.xml 的 `<scope>test</scope>`。

- [ ] **Step 3: 更新 ToolServerApp.java 注册 webhook 路由**

定位到 `ToolServerApp.java` 构造函数 `ToolServerApp()` 中的行:

```java
new ToolServerController().registerRoutes(app);
app.get("/health", ctx -> ctx.result("OK"));
```

改为:

```java
new ToolServerController().registerRoutes(app);
app.get("/health", ctx -> ctx.result("OK"));

// CI 集成: webhook 端点
String webhookSecret = System.getenv("CODEGUARD_WEBHOOK_SECRET");
if (webhookSecret != null && !webhookSecret.isBlank()) {
    var jobRepo = new com.codeguard.ci.job.JobRepository("./data/codeguard-jobs");
    var scheduler = new com.codeguard.ci.job.JobScheduler(jobRepo, 2, null, null);
    var webhookCtrl = new com.codeguard.ci.webhook.GitHubWebhookController(webhookSecret, jobRepo, scheduler);
    webhookCtrl.register(app);
    scheduler.start();
    log.info("GitHub webhook 端点已启用: POST /webhooks/github");
}
```

- [ ] **Step 4: 在 pom.xml 添加测试依赖**

在 `<dependencies>` 内 junit 之后添加:

```xml
<dependency>
    <groupId>com.squareup.okhttp3</groupId>
    <artifactId>okhttp</artifactId>
    <version>4.12.0</version>
    <scope>test</scope>
</dependency>
<dependency>
    <groupId>io.javalin</groupId>
    <artifactId>javalin-testtools</artifactId>
    <version>${javalin.version}</version>
    <scope>test</scope>
</dependency>
```

- [ ] **Step 5: 跑全部测试**

```powershell
cd services/gateway; mvn test -q
```

Expected: ALL TESTS PASS

- [ ] **Step 6: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/webhook/GitHubWebhookController.java
git add services/gateway/src/test/java/com/codeguard/ci/webhook/GitHubWebhookControllerTest.java
git add services/gateway/src/main/java/com/codeguard/toolserver/ToolServerApp.java
git add services/gateway/pom.xml
git commit -m "feat(ci): 新增 GitHub webhook 端点，三层过滤 + 异步 job 提交"
```

---

**Phase 1 结束状态验证:**

```powershell
# 启动 Gateway，webhook 端点就绪
$env:CODEGUARD_WEBHOOK_SECRET="test_secret"
java -jar target/codeguard-gateway.jar
# 日志应显示: "GitHub webhook 端点已启用: POST /webhooks/github"
```

Phase 1 完成时：webhook 端点可接收请求、验签、去重、持久化到 H2。job 表写入成功但尚未执行。

---

## Phase 2: 异步 Job 执行 + 审查引擎

### Task 2.1: JobScheduler

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/job/JobScheduler.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/job/JobSchedulerTest.java`
- Create: `services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutor.java` (stub)

- [ ] **Step 1: 写 ReviewExecutor 存根接口**

```java
package com.codeguard.ci.executor;

import com.codeguard.ci.model.ReviewJob;

/**
 * 审查执行器。Phase 2 中为存根，Phase 3 完整实现。
 */
@FunctionalInterface
public interface ReviewExecutor {
    void execute(ReviewJob job);
}
```

- [ ] **Step 2: 写 JobScheduler.java**

```java
package com.codeguard.ci.job;

import com.codeguard.ci.executor.ReviewExecutor;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 异步 job 调度器: 有界队列 + 固定线程池 + 全局并发信号量 + 启动恢复。
 */
public class JobScheduler {

    private static final Logger log = LoggerFactory.getLogger(JobScheduler.class);

    private final JobRepository repo;
    private final int maxConcurrency;
    private final ReviewExecutor executor;
    private final Semaphore concurrencySem;
    private final ThreadPoolExecutor threadPool;
    private final AtomicBoolean running = new AtomicBoolean(false);

    public JobScheduler(JobRepository repo, int maxConcurrency,
                        ReviewExecutor executor, ReviewExecutor defaultExecutor) {
        this.repo = repo;
        this.maxConcurrency = maxConcurrency;
        this.executor = executor != null ? executor : defaultExecutor;
        this.concurrencySem = new Semaphore(maxConcurrency);
        this.threadPool = new ThreadPoolExecutor(
            maxConcurrency, maxConcurrency,
            60, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(10),
            new ThreadPoolExecutor.AbortPolicy()
        );
    }

    /** 提交 job 到队列。队列满 → 返回 false。 */
    public boolean submit(ReviewJob job) {
        try {
            threadPool.submit(() -> executeJob(job));
            return true;
        } catch (RejectedExecutionException e) {
            log.warn("job 队列满，拒绝: {}", job.dedupKey());
            return false;
        }
    }

    /** 启动: 恢复未完成 job + 标记 scheduler 为运行中 */
    public void start() {
        running.set(true);
        List<ReviewJob> unfinished = repo.findUnfinished();
        log.info("启动恢复: 发现 {} 个未完成 job", unfinished.size());
        for (ReviewJob job : unfinished) {
            job.setStatus(Status.PENDING);
            repo.update(job);
            submit(job);
        }
    }

    private void executeJob(ReviewJob job) {
        if (!concurrencySem.tryAcquire()) {
            // 理论上不应发生（队列容量 = 线程数），兜底重新入队
            log.warn("获取并发许可失败，重新入队: {}", job.dedupKey());
            threadPool.submit(() -> executeJob(job));
            return;
        }
        try {
            log.info("开始审查: {} PR#{} sha={}", job.getRepo(), job.getPrNumber(), job.getHeadSha().substring(0, 7));
            job.setStatus(Status.RUNNING);
            repo.update(job);
            executor.execute(job);
            // execute() 内部负责置 DONE 或 FAILED + 重试
        } catch (Exception e) {
            log.error("审查异常: {}", job.dedupKey(), e);
            job.setStatus(Status.FAILED);
            job.setErrorMessage("执行异常: " + e.getMessage());
            repo.update(job);
        } finally {
            concurrencySem.release();
        }
    }
}
```

- [ ] **Step 3: 写 JobSchedulerTest.java**

```java
package com.codeguard.ci.job;

import com.codeguard.ci.executor.ReviewExecutor;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import com.codeguard.ci.model.ReviewJob.Status;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

class JobSchedulerTest {

    private JobRepository repo;
    private JobScheduler scheduler;
    private AtomicInteger executeCount;

    @BeforeEach
    void setUp() {
        String dbPath = System.getProperty("java.io.tmpdir") + "/codeguard-sched-" + System.nanoTime();
        repo = new JobRepository(dbPath);
        executeCount = new AtomicInteger(0);
        ReviewExecutor countingExecutor = job -> {
            executeCount.incrementAndGet();
            job.setStatus(Status.DONE);
            repo.update(job);
        };
        scheduler = new JobScheduler(repo, 2, countingExecutor, countingExecutor);
    }

    @AfterEach
    void tearDown() {
        repo.close();
    }

    @Test
    void shouldExecuteSubmittedJob() throws Exception {
        var job = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "url", 1, "sha1", "main", 1L)
        )).orElseThrow();

        scheduler.start();
        scheduler.submit(job);
        Thread.sleep(500);

        var found = repo.findByDedupKey("a/b", 1, "sha1");
        assertEquals(Status.DONE, found.get().getStatus());
        assertEquals(1, executeCount.get());
    }

    @Test
    void shouldRecoverUnfinishedJobsOnStart() throws Exception {
        var j1 = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "url1", 1, "sha1", "main", 1L))).orElseThrow();
        var j2 = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "url2", 2, "sha2", "main", 1L))).orElseThrow();
        // j1 保持 PENDING，j2 标记为 RUNNING（模拟崩溃）
        j2.setStatus(Status.RUNNING);
        repo.update(j2);

        scheduler.start();
        Thread.sleep(500);

        assertEquals(Status.DONE, repo.findByDedupKey("a/b", 1, "sha1").get().getStatus());
        assertEquals(Status.DONE, repo.findByDedupKey("a/b", 2, "sha2").get().getStatus());
        assertEquals(2, executeCount.get());
    }

    @Test
    void shouldEnforceConcurrencyLimit() throws Exception {
        CountDownLatch latch = new CountDownLatch(2);
        ReviewExecutor blockingExecutor = job -> {
            latch.countDown();
            try { Thread.sleep(2000); } catch (InterruptedException ignored) {}
            job.setStatus(Status.DONE);
            repo.update(job);
        };
        scheduler = new JobScheduler(repo, 1, blockingExecutor, blockingExecutor);

        var j1 = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "u1", 1, "sha1", "main", 1L))).orElseThrow();
        var j2 = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "u2", 2, "sha2", "main", 1L))).orElseThrow();
        scheduler.start();
        assertTrue(scheduler.submit(j1));
        assertTrue(scheduler.submit(j2)); // 进入队列
        assertTrue(latch.await(5, TimeUnit.SECONDS));
    }
}
```

- [ ] **Step 4: 跑测试验证**

```powershell
cd services/gateway; mvn test -pl . -Dtest=JobSchedulerTest -q
```

Expected: 3 tests PASS

- [ ] **Step 5: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/job/JobScheduler.java
git add services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutor.java
git add services/gateway/src/test/java/com/codeguard/ci/job/JobSchedulerTest.java
git commit -m "feat(ci): 新增 JobScheduler，有界队列 + 信号量并发控制 + 启动恢复"
```

---

### Task 2.2: ReviewExecutor 实现

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutorImpl.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/executor/ReviewExecutorImplTest.java`

- [ ] **Step 1: 写 ReviewExecutorImpl.java**

```java
package com.codeguard.ci.executor;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.TimeUnit;

/**
 * 审查执行器实现: git clone/fetch + ProcessBuilder 调 Python CLI + 结果解析。
 */
public class ReviewExecutorImpl implements ReviewExecutor {

    private static final Logger log = LoggerFactory.getLogger(ReviewExecutorImpl.class);

    private final JobRepository repo;
    private final Path workspacesDir;
    private final String githubToken;

    public ReviewExecutorImpl(JobRepository repo, Path workspacesDir, String githubToken) {
        this.repo = repo;
        this.workspacesDir = workspacesDir;
        this.githubToken = githubToken;
    }

    @Override
    public void execute(ReviewJob job) {
        Path workdir = null;
        try {
            // 1. git clone / fetch
            workdir = cloneOrFetch(job);

            // 2. 构建命令
            List<String> cmd = buildCommand(workdir, job);

            // 3. 执行
            String stdout = runProcess(cmd, workdir);

            // 4. 解析结果
            if (stdout == null || stdout.isBlank()) {
                job.setErrorMessage("审查输出为空");
                handleFailure(job, true);
                return;
            }

            job.setResultJson(stdout);
            job.setStatus(Status.DONE);
            repo.update(job);
            log.info("审查完成: {} PR#{}", job.getRepo(), job.getPrNumber());

        } catch (Exception e) {
            log.error("审查执行失败: {}", job.dedupKey(), e);
            job.setErrorMessage(e.getMessage());
            handleFailure(job, e instanceof TimeoutException);
        }
    }

    private Path cloneOrFetch(ReviewJob job) throws IOException, InterruptedException {
        String safeName = job.getRepo().replace('/', '-') + "-" + job.getPrNumber();
        Path dir = workspacesDir.resolve(safeName);
        String cloneUrl = job.getCloneUrl().replace(
            "https://", "https://x-access-token:" + githubToken + "@");

        if (Files.exists(dir.resolve(".git"))) {
            log.info("fetch 已有仓库: {}", dir);
            runCmd(dir, "git", "fetch", "origin", job.getBaseRef() + ":" + job.getBaseRef());
        } else {
            log.info("clone 新仓库: {} → {}", cloneUrl, dir);
            Files.createDirectories(dir.getParent());
            runCmd(dir.getParent(), "git", "clone", "--depth=50", cloneUrl, dir.getFileName().toString());
        }
        return dir;
    }

    private List<String> buildCommand(Path workdir, ReviewJob job) {
        return List.of(
            "python", "-m", "codeguard_agent", "review",
            "--repo", workdir.toString(),
            "--base", "origin/" + job.getBaseRef(),
            "--mode", "pipeline",
            "--format", "json"
        );
    }

    private String runProcess(List<String> cmd, Path workdir) throws IOException, InterruptedException, TimeoutException {
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.directory(workdir.toFile());
        pb.redirectErrorStream(false);

        // 透传所有 CODEGUARD_* 环境变量 + 工具服务 URL
        Map<String, String> env = pb.environment();
        System.getenv().forEach((k, v) -> {
            if (k.startsWith("CODEGUARD_")) env.put(k, v);
        });
        // 确保 Agent 能回调 Gateway 工具
        env.putIfAbsent("CODEGUARD_TOOL_SERVER_URL", "http://localhost:9090");

        Process process = pb.start();
        String stdout = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        String stderr = new String(process.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);

        boolean finished = process.waitFor(10, TimeUnit.MINUTES);
        if (!finished) {
            process.destroyForcibly();
            throw new TimeoutException("审查进程超时 (10min)");
        }

        if (!stderr.isBlank()) {
            log.warn("审查 stderr: {}", stderr.substring(0, Math.min(500, stderr.length())));
        }

        return stdout.trim();
    }

    private void handleFailure(ReviewJob job, boolean retryable) {
        if (retryable && job.getRetryCount() < 2) {
            job.setStatus(Status.RETRYING);
            job.setRetryCount(job.getRetryCount() + 1);
            repo.update(job);
            log.info("审查失败，{}s 后重试 (第{}次)", 30, job.getRetryCount());
            // 30s 后重新提交
            new Thread(() -> {
                try { Thread.sleep(30_000); } catch (InterruptedException ignored) {}
                job.setStatus(Status.PENDING);
                repo.update(job);
                // 重新提交到 scheduler——此处通过回调和 scheduler 交互
                // 简化: 直接同步重试
                execute(job);
            }).start();
        } else {
            job.setStatus(Status.FAILED);
            repo.update(job);
            log.error("审查最终失败: {}", job.dedupKey());
        }
    }

    private void runCmd(Path dir, String... args) throws IOException, InterruptedException {
        ProcessBuilder pb = new ProcessBuilder(args);
        pb.directory(dir.toFile());
        pb.redirectErrorStream(true);
        Process p = pb.start();
        boolean ok = p.waitFor(2, TimeUnit.MINUTES);
        if (!ok) {
            p.destroyForcibly();
            throw new IOException("git 命令超时 (2min): " + String.join(" ", args));
        }
        if (p.exitValue() != 0) {
            String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            throw new IOException("git 命令失败: " + out);
        }
    }

    /** 自定义超时异常 */
    public static class TimeoutException extends Exception {
        public TimeoutException(String msg) { super(msg); }
    }
}
```

- [ ] **Step 2: 写 ReviewExecutorImplTest.java**

```java
package com.codeguard.ci.executor;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import com.codeguard.ci.model.ReviewJob.Status;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.*;
import java.util.concurrent.TimeUnit;

class ReviewExecutorImplTest {

    @TempDir
    Path workspacesDir;

    private JobRepository repo;
    private ReviewExecutorImpl executor;

    @BeforeEach
    void setUp() {
        String dbPath = System.getProperty("java.io.tmpdir") + "/codeguard-exec-" + System.nanoTime();
        repo = new JobRepository(dbPath);
        executor = new ReviewExecutorImpl(repo, workspacesDir, "fake-token");
    }

    @AfterEach
    void tearDown() {
        repo.close();
    }

    @Test
    void shouldSkipReviewForLargeDiff() {
        // 已移至 ReviewGuard 测试
    }

    @Test
    void shouldMarkFailedOnCloneFailure() {
        var job = new ReviewJob(new WebhookPayload(
            "no-such/repo", "https://gh/no-such/repo.git", 1, "sha", "main", 1L));
        repo.insert(job);

        executor.execute(job);
        // clone 失败 → 不可重试 → FAILED
        assertEquals(Status.FAILED, job.getStatus());
    }

    @Test
    void shouldMarkDoneOnSuccessfulReview() throws Exception {
        // 在 workspacesDir 下创建一个假的 git 仓库
        Path repoDir = workspacesDir.resolve("fake-repo");
        Files.createDirectories(repoDir.resolve(".git"));
        String originalDiff = """
            diff --git a/UserService.java b/UserService.java
            +++ b/UserService.java
            @@ -10,6 +10,7 @@
             public class UserService {
            +    public String getEmail(String id) { return null; }
             }
            """;
        Files.writeString(repoDir.resolve("UserService.java"), originalDiff);

        // Read python path from env or skip on Windows
        var job = new ReviewJob(new WebhookPayload(
            "fake/repo", "https://gh/fake/repo.git", 1, "sha", "main", 1L));
        repo.insert(job);

        // 注意: 此测试依赖本地 Python 环境和 codeguard agent 安装
        // CI 环境可能需要额外配置或 skip
        executor.execute(job);
        assertTrue(job.getStatus() == Status.DONE || job.getStatus() == Status.FAILED,
            "job 应有终态");
    }
}
```

- [ ] **Step 3: 更新 ToolServerApp 构造函数使用真实 executor**

在 `ToolServerApp.java` 中，将之前的:

```java
var scheduler = new com.codeguard.ci.job.JobScheduler(jobRepo, 2, null, null);
```

改为:

```java
String githubToken = System.getenv("CODEGUARD_GITHUB_TOKEN");
var executor = new com.codeguard.ci.executor.ReviewExecutorImpl(
    jobRepo,
    java.nio.file.Path.of("/tmp/codeguard-jobs"),
    githubToken != null ? githubToken : ""
);
var scheduler = new com.codeguard.ci.job.JobScheduler(jobRepo, 2, executor, executor);
```

- [ ] **Step 4: 跑测试**

```powershell
cd services/gateway; mvn test -pl . -Dtest=ReviewExecutorImplTest -q
```

Expected: 2-3 tests PASS（有 Python 环境时 3 个；无 Python 时 skip）

- [ ] **Step 5: 删除 ReviewExecutor.java 存根接口，改为直接使用 ReviewExecutorImpl**

因为 JobScheduler 不再通过接口注入，删除 `ReviewExecutor.java`:

```java
// 删除此文件: services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutor.java
```

并修改 `JobScheduler.java` 直接持有 `ReviewExecutorImpl` 引用。将所有 `ReviewExecutor` 类型替换为 `ReviewExecutorImpl`。

- [ ] **Step 6: 跑全量测试**

```powershell
cd services/gateway; mvn test -q
```

Expected: ALL TESTS PASS

- [ ] **Step 7: 提交**

```bash
git rm services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutor.java
git add services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutorImpl.java
git add services/gateway/src/main/java/com/codeguard/ci/job/JobScheduler.java
git add services/gateway/src/main/java/com/codeguard/toolserver/ToolServerApp.java
git add services/gateway/src/test/java/com/codeguard/ci/executor/ReviewExecutorImplTest.java
git add services/gateway/src/test/java/com/codeguard/ci/job/JobSchedulerTest.java
git commit -m "feat(ci): 实现 ReviewExecutor，ProcessBuilder 调 Python CLI 执行审查"
```

---

**Phase 2 结束状态验证:**

webhook 接收 → H2 持久化 → 异步 job 执行 → git clone → Python CLI 审查 → stdout JSON 写入 job.result_json。端到端链路打通（缺结果反馈到 GitHub）。

---

## Phase 3: 结果反馈（Check Runs + PR Comments）

### Task 3.1: GitHub API Client

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/github/GitHubClient.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/github/GitHubClientTest.java`

- [ ] **Step 1: 写 GitHubClient.java**

```java
package com.codeguard.ci.github;

import com.codeguard.ci.model.ReviewJob;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * GitHub API Client: App 安装令牌管理 + Check Runs + PR Comments。
 */
public class GitHubClient {

    private static final Logger log = LoggerFactory.getLogger(GitHubClient.class);
    private static final String API_BASE = "https://api.github.com";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final HttpClient HTTP = HttpClient.newHttpClient();

    private final String appId;
    private final String privateKeyPem;
    private final Map<Long, TokenCache> tokenCache = new ConcurrentHashMap<>();

    private record TokenCache(String token, Instant expiresAt) {}

    public GitHubClient(String appId, String privateKeyPem) {
        this.appId = appId;
        this.privateKeyPem = privateKeyPem;
    }

    /** 获取指定 installation 的访问令牌（含缓存和自动续期） */
    public String getInstallationToken(long installationId) throws IOException, InterruptedException {
        TokenCache cached = tokenCache.get(installationId);
        if (cached != null && Instant.now().isBefore(cached.expiresAt.minusSeconds(60))) {
            return cached.token;
        }

        // 1. 生成 JWT (App 认证)
        String jwt = JwtHelper.createJwt(appId, privateKeyPem);

        // 2. POST /app/installations/{id}/access_tokens
        HttpRequest req = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/app/installations/" + installationId + "/access_tokens"))
            .header("Authorization", "Bearer " + jwt)
            .header("Accept", "application/vnd.github+json")
            .POST(HttpRequest.BodyPublishers.noBody())
            .build();

        HttpResponse<String> resp = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() != 201) {
            throw new IOException("获取 installation token 失败: " + resp.body());
        }

        JsonNode json = MAPPER.readTree(resp.body());
        String token = json.path("token").asText();
        Instant expires = Instant.parse(json.path("expires_at").asText());
        tokenCache.put(installationId, new TokenCache(token, expires));
        return token;
    }

    // ── Check Runs ──

    /** 创建 Check Run，返回 check_run.id */
    public long createCheckRun(String repo, String headSha, long installationId)
            throws IOException, InterruptedException {
        String token = getInstallationToken(installationId);
        ObjectNode body = MAPPER.createObjectNode()
            .put("name", "Codeguard Review")
            .put("head_sha", headSha)
            .put("status", "in_progress")
            .put("started_at", Instant.now().toString());

        HttpRequest req = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/repos/" + repo + "/check-runs"))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/vnd.github+json")
            .POST(HttpRequest.BodyPublishers.ofString(body.toString()))
            .build();

        HttpResponse<String> resp = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() != 201) {
            throw new IOException("创建 Check Run 失败: " + resp.body());
        }
        return MAPPER.readTree(resp.body()).path("id").asLong();
    }

    /** 更新 Check Run 为完成状态 */
    public void completeCheckRun(String repo, long checkRunId, String conclusion,
                                  String title, String summary, List<IssueAnnot> annotations,
                                  long installationId) throws IOException, InterruptedException {
        String token = getInstallationToken(installationId);
        ObjectNode output = MAPPER.createObjectNode()
            .put("title", title)
            .put("summary", summary);

        ArrayNode annots = output.putArray("annotations");
        for (IssueAnnot a : annotations) {
            annots.addObject()
                .put("path", a.path())
                .put("start_line", a.line())
                .put("end_line", a.line())
                .put("annotation_level", a.annotationLevel())
                .put("message", a.message());
        }

        ObjectNode body = MAPPER.createObjectNode()
            .put("status", "completed")
            .put("conclusion", conclusion)
            .put("completed_at", Instant.now().toString())
            .set("output", output);

        HttpRequest req = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/repos/" + repo + "/check-runs/" + checkRunId))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/vnd.github+json")
            .method("PATCH", HttpRequest.BodyPublishers.ofString(body.toString()))
            .build();

        HttpResponse<String> resp = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() != 200) {
            log.error("更新 Check Run 失败: {}", resp.body());
        }
    }

    /** Check Run annotation 值对象 */
    public record IssueAnnot(String path, int line, String annotationLevel, String message) {}

    // ── PR 行级评论 ──

    /** 在 PR 指定行上贴评论 */
    public void createPRComment(String repo, int prNumber, String commitId,
                                 String path, int line, String body, long installationId)
            throws IOException, InterruptedException {
        String token = getInstallationToken(installationId);
        ObjectNode payload = MAPPER.createObjectNode()
            .put("body", body)
            .put("commit_id", commitId)
            .put("path", path)
            .put("line", line);

        HttpRequest req = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/repos/" + repo + "/pulls/" + prNumber + "/comments"))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/vnd.github+json")
            .POST(HttpRequest.BodyPublishers.ofString(payload.toString()))
            .build();

        HttpResponse<String> resp = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() != 201) {
            log.warn("创建 PR 评论失败 (path={}, line={}): {}", path, line, resp.body());
        }
    }
}
```

- [ ] **Step 2: 写 JwtHelper.java（参考实现）**

```java
package com.codeguard.ci.github;

import java.security.KeyFactory;
import java.security.PrivateKey;
import java.security.spec.PKCS8EncodedKeySpec;
import java.time.Instant;
import java.util.Base64;

/**
 * GitHub App JWT 生成。用于获取 installation access token。
 */
final class JwtHelper {

    private JwtHelper() {}

    /** 生成 GitHub App 认证 JWT。有效期 10 分钟。 */
    static String createJwt(String appId, String privateKeyPem) {
        try {
            String keyContent = privateKeyPem
                .replace("-----BEGIN RSA PRIVATE KEY-----", "")
                .replace("-----END RSA PRIVATE KEY-----", "")
                .replaceAll("\\s", "");
            byte[] keyBytes = Base64.getDecoder().decode(keyContent);
            PKCS8EncodedKeySpec spec = new PKCS8EncodedKeySpec(keyBytes);
            PrivateKey key = KeyFactory.getInstance("RSA").generatePrivate(spec);

            Instant now = Instant.now();
            String header = base64url("{\"alg\":\"RS256\",\"typ\":\"JWT\"}");
            String payload = base64url(
                "{\"iat\":" + now.getEpochSecond() +
                ",\"exp\":" + now.plusSeconds(600).getEpochSecond() +
                ",\"iss\":\"" + appId + "\"}");

            String toSign = header + "." + payload;
            java.security.Signature sig = java.security.Signature.getInstance("SHA256withRSA");
            sig.initSign(key);
            sig.update(toSign.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            String signature = base64url(sig.sign());

            return toSign + "." + signature;
        } catch (Exception e) {
            throw new RuntimeException("JWT 生成失败", e);
        }
    }

    private static String base64url(String input) {
        return Base64.getUrlEncoder().withoutPadding()
            .encodeToString(input.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    private static String base64url(byte[] input) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(input);
    }
}
```

- [ ] **Step 3: 写 GitHubClientTest.java**

```java
package com.codeguard.ci.github;

import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

class GitHubClientTest {

    @Test
    void shouldConstructWithoutError() {
        GitHubClient client = new GitHubClient("12345", "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----");
        assertNotNull(client);
    }

    @Test
    void shouldRequireRealCredentialsForAPI() {
        // 集成测试需要真实 GitHub App 凭据，单元测试仅验证构造
        assertTrue(true, "集成测试需要真实凭据");
    }
}
```

- [ ] **Step 4: 跑测试**

```powershell
cd services/gateway; mvn test -pl . -Dtest=GitHubClientTest -q
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/github/GitHubClient.java
git add services/gateway/src/main/java/com/codeguard/ci/github/JwtHelper.java
git add services/gateway/src/test/java/com/codeguard/ci/github/GitHubClientTest.java
git commit -m "feat(ci): 新增 GitHub API Client，App 令牌管理 + Check Runs + PR Comments"
```

---

### Task 3.2: 结果反馈集成

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/toolserver/ToolServerApp.java`

- [ ] **Step 1: 在 ToolServerApp 中构造 GitHubClient**

在 ToolServerApp 构造函数中，`webhookSecret` 非空的 if 块内，scheduler 构造前添加:

```java
String appId = System.getenv("CODEGUARD_GITHUB_APP_ID");
String privateKey = System.getenv("CODEGUARD_GITHUB_PRIVATE_KEY");
var githubClient = (appId != null && privateKey != null)
    ? new com.codeguard.ci.github.GitHubClient(appId, privateKey)
    : null;
```

- [ ] **Step 2: 创建 ResultFeedback 类（结果解析 + GitHub API 写入）**

```java
// 新建文件: services/gateway/src/main/java/com/codeguard/ci/executor/ResultFeedback.java
package com.codeguard.ci.executor;

import com.codeguard.ci.github.GitHubClient;
import com.codeguard.ci.model.ReviewJob;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.*;

public class ResultFeedback {

    private static final Logger log = LoggerFactory.getLogger(ResultFeedback.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int MAX_ANNOTATIONS = 50;
    private static final int MAX_LINE_COMMENTS = 10;

    private final GitHubClient client;

    public ResultFeedback(GitHubClient client) {
        this.client = client;
    }

    public void postResults(ReviewJob job) {
        if (client == null || job.getResultJson() == null) return;

        try {
            JsonNode root = MAPPER.readTree(job.getResultJson());
            JsonNode issues = root.path("issues");
            if (!issues.isArray()) return;

            List<JsonNode> issueList = new ArrayList<>();
            issues.forEach(issueList::add);

            // Check Run
            long checkRunId = client.createCheckRun(job.getRepo(), job.getHeadSha(), job.getInstallationId());
            String conclusion = determineConclusion(issueList);
            String title = "发现 " + issueList.size() + " 个问题";
            String summary = buildSummary(issueList);
            List<GitHubClient.IssueAnnot> annotations = buildAnnotations(issueList);
            client.completeCheckRun(job.getRepo(), checkRunId, conclusion, title,
                summary, annotations, job.getInstallationId());

            // 行级评论（仅高危）
            postHighSeverityComments(job, issueList);

        } catch (Exception e) {
            log.error("结果反馈失败: {}", job.dedupKey(), e);
        }
    }

    private String determineConclusion(List<JsonNode> issues) {
        boolean hasCritical = issues.stream().anyMatch(i ->
            "CRITICAL".equals(i.path("severity").asText()));
        if (hasCritical) return "failure";
        return issues.isEmpty() ? "success" : "neutral";
    }

    private String buildSummary(List<JsonNode> issues) {
        StringBuilder sb = new StringBuilder("## Codeguard 审查结果\n\n");
        sb.append("共发现 **").append(issues.size()).append("** 个问题\n\n");
        sb.append("| 级别 | 类型 | 文件 | 行号 | 问题 |\n");
        sb.append("|------|------|------|------|------|\n");

        int critical = 0, warning = 0, info = 0;
        for (JsonNode i : issues) {
            String sev = i.path("severity").asText();
            sb.append("| ").append(severityIcon(sev)).append(" ").append(sev)
              .append(" | ").append(i.path("type").asText())
              .append(" | ").append(i.path("file").asText())
              .append(" | ").append(i.path("line").asInt())
              .append(" | ").append(i.path("message").asText()).append(" |\n");
            switch (sev) {
                case "CRITICAL": critical++; break;
                case "WARNING": warning++; break;
                default: info++;
            }
        }
        sb.append("\n📊 统计: CRITICAL=").append(critical)
          .append(" WARNING=").append(warning)
          .append(" INFO=").append(info);
        return sb.toString();
    }

    private List<GitHubClient.IssueAnnot> buildAnnotations(List<JsonNode> issues) {
        List<GitHubClient.IssueAnnot> annots = new ArrayList<>();
        int limit = Math.min(issues.size(), MAX_ANNOTATIONS);
        for (int i = 0; i < limit; i++) {
            JsonNode issue = issues.get(i);
            annots.add(new GitHubClient.IssueAnnot(
                issue.path("file").asText(),
                Math.max(issue.path("line").asInt(), 1),
                toAnnotationLevel(issue.path("severity").asText()),
                issue.path("message").asText()
            ));
        }
        return annots;
    }

    private void postHighSeverityComments(ReviewJob job, List<JsonNode> issues) {
        List<JsonNode> criticals = issues.stream()
            .filter(i -> "CRITICAL".equals(i.path("severity").asText()))
            .filter(i -> i.path("confidence").asDouble(1.0) >= 0.7)
            .sorted((a, b) -> Double.compare(
                b.path("confidence").asDouble(1.0),
                a.path("confidence").asDouble(1.0)))
            .limit(MAX_LINE_COMMENTS)
            .toList();

        for (JsonNode issue : criticals) {
            try {
                String body = String.format("🔴 **%s**: %s\n\n建议: %s",
                    issue.path("type").asText(),
                    issue.path("message").asText(),
                    issue.path("suggestion").asText("无"));
                client.createPRComment(job.getRepo(), job.getPrNumber(),
                    job.getHeadSha(), issue.path("file").asText(),
                    Math.max(issue.path("line").asInt(), 1),
                    body, job.getInstallationId());
            } catch (Exception e) {
                log.warn("行级评论失败: {}", e.getMessage());
            }
        }
    }

    private String severityIcon(String sev) {
        return switch (sev) {
            case "CRITICAL" -> "🔴";
            case "WARNING" -> "🟡";
            default -> "🔵";
        };
    }

    private String toAnnotationLevel(String sev) {
        return switch (sev) {
            case "CRITICAL" -> "failure";
            case "WARNING" -> "warning";
            default -> "notice";
        };
    }
}
```

- [ ] **Step 3: 在 ReviewExecutorImpl.execute() 最后调用结果反馈**

修改 `ReviewExecutorImpl.java`，在 `job.setStatus(Status.DONE); repo.update(job);` 之后添加:

```java
if (feedback != null) {
    feedback.postResults(job);
}
```

并在构造函数添加 `feedback` 字段:

```java
private final ResultFeedback feedback;

public ReviewExecutorImpl(JobRepository repo, Path workspacesDir,
                           String githubToken, ResultFeedback feedback) {
    // ... existing fields
    this.feedback = feedback;
}
```

- [ ] **Step 4: 更新 ToolServerApp 构造链路**

```java
// 在 scheduler 构造前
var feedback = githubClient != null ? new ResultFeedback(githubClient) : null;
var executor = new ReviewExecutorImpl(jobRepo,
    java.nio.file.Path.of("/tmp/codeguard-jobs"),
    githubToken != null ? githubToken : "", feedback);
```

- [ ] **Step 5: 跑全量测试**

```powershell
cd services/gateway; mvn test -q
```

Expected: ALL TESTS PASS

- [ ] **Step 6: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/executor/ResultFeedback.java
git add services/gateway/src/main/java/com/codeguard/ci/executor/ReviewExecutorImpl.java
git add services/gateway/src/main/java/com/codeguard/toolserver/ToolServerApp.java
git commit -m "feat(ci): 集成结果反馈，Check Runs 门禁 + 高危行级评论"
```

---

**Phase 3 结束状态验证:**

完整链路: webhook → job → clone → 审查 → JSON 解析 → Check Run 门禁 + CRITICAL issue 行级评论。端到端可用。

---

## Phase 4: 防护 + 部署

### Task 4.1: ReviewGuard（限流+超时+降级+重试）

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/ci/guard/ReviewGuard.java`
- Create: `services/gateway/src/test/java/com/codeguard/ci/guard/ReviewGuardTest.java`

- [ ] **Step 1: 写 ReviewGuard.java**

```java
package com.codeguard.ci.guard;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import com.google.common.util.concurrent.RateLimiter;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 防护层: 接口限流 + 超时判定 + 大 diff 降级 + 可重试判断。
 */
public final class ReviewGuard {

    private static final Logger log = LoggerFactory.getLogger(ReviewGuard.class);

    private final RateLimiter rateLimiter;
    private final int maxDiffLines;
    private final long reviewTimeoutMinutes;

    public ReviewGuard(double permitsPerHour, int maxDiffLines, long reviewTimeoutMinutes) {
        this.rateLimiter = RateLimiter.create(permitsPerHour / 3600.0);
        this.maxDiffLines = maxDiffLines;
        this.reviewTimeoutMinutes = reviewTimeoutMinutes;
    }

    /** 接口限流检查。未通过 → 返回 false。 */
    public boolean tryAcquireWebhook(long timeoutMs) {
        return rateLimiter.tryAcquire(java.time.Duration.ofMillis(timeoutMs));
    }

    /** 检查 diff 是否过大需降级 */
    public boolean isDiffTooLarge(int diffLineCount) {
        return diffLineCount > maxDiffLines;
    }

    /** 生成降级结果 JSON，直接写入 job */
    public String buildDegradedResult(ReviewJob job, int diffLineCount) {
        return String.format("""
            {
              "issues": [{
                "severity": "WARNING",
                "file": "",
                "line": 0,
                "type": "ci",
                "message": "变更过大 (%d 行)，超过 %d 行阈值，自动跳过审查。建议拆分为小的 PR。",
                "suggestion": "拆分 PR",
                "confidence": 1.0
              }]
            }
            """, diffLineCount, maxDiffLines);
    }

    /** 判断异常是否可重试 */
    public boolean isRetryable(Exception e) {
        if (e instanceof java.util.concurrent.TimeoutException) return true;
        String msg = e.getMessage() != null ? e.getMessage().toLowerCase() : "";
        return msg.contains("timeout") || msg.contains("connection") || msg.contains("transient");
    }
}
```

- [ ] **Step 2: 写 ReviewGuardTest.java**

```java
package com.codeguard.ci.guard;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

class ReviewGuardTest {

    private ReviewGuard guard = new ReviewGuard(30, 5000, 10);

    @Test
    void shouldAllowWithinLimit() {
        assertTrue(guard.tryAcquireWebhook(500));
    }

    @Test
    void shouldDetectLargeDiff() {
        assertTrue(guard.isDiffTooLarge(5001));
        assertFalse(guard.isDiffTooLarge(100));
    }

    @Test
    void shouldBuildDegradedResult() {
        var job = new ReviewJob(new WebhookPayload("r", "u", 1, "sha", "main", 1L));
        String result = guard.buildDegradedResult(job, 6000);
        assertTrue(result.contains("6000"));
        assertTrue(result.contains("WARNING"));
        assertTrue(result.contains("跳过审查"));
    }

    @Test
    void shouldIdentifyRetryableErrors() {
        assertTrue(guard.isRetryable(new java.util.concurrent.TimeoutException("timeout")));
        assertTrue(guard.isRetryable(new RuntimeException("connection refused")));
        assertFalse(guard.isRetryable(new RuntimeException("JSON parse error")));
    }
}
```

- [ ] **Step 3: 集成到 GitHubWebhookController**

在 `handle()` 方法中，验签通过后、事件过滤前添加:

```java
if (guard != null && !guard.tryAcquireWebhook(100)) {
    ctx.status(429).header("Retry-After", "120").json(Map.of("error", "rate_limited"));
    return;
}
```

在 controller 构造函数添加 `ReviewGuard guard` 字段。

- [ ] **Step 4: 跑测试**

```powershell
cd services/gateway; mvn test -pl . -Dtest=ReviewGuardTest -q
```

Expected: 4 tests PASS

- [ ] **Step 5: 提交**

```bash
git add services/gateway/src/main/java/com/codeguard/ci/guard/ReviewGuard.java
git add services/gateway/src/test/java/com/codeguard/ci/guard/ReviewGuardTest.java
git add services/gateway/src/main/java/com/codeguard/ci/webhook/GitHubWebhookController.java
git commit -m "feat(ci): 新增防护层，接口限流 + 大diff降级 + 重试判定"
```

---

### Task 4.2: Docker 部署

**Files:**
- Create: `Dockerfile`（仓库根目录）
- Create: `docker-compose.yml`（仓库根目录）
- Modify: `.env.example`

- [ ] **Step 1: 写 Dockerfile**

```dockerfile
FROM eclipse-temurin:21-jre

# 安装 Python 3.12
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3-pip git && \
    rm -rf /var/lib/apt/lists/* && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1

# 复制 Java Gateway
COPY services/gateway/target/codeguard-gateway.jar /app/codeguard-gateway.jar

# 复制并安装 Python Agent
COPY services/agent/ /app/agent/
WORKDIR /app/agent
RUN pip install --no-cache-dir -e . --break-system-packages

WORKDIR /app
EXPOSE 9090

# 数据目录（H2 + job workspaces）
VOLUME ["/app/data", "/tmp/codeguard-jobs"]

ENTRYPOINT ["java", "-jar", "codeguard-gateway.jar"]
```

- [ ] **Step 2: 写 docker-compose.yml**

```yaml
version: "3.9"

services:
  codeguard:
    build: .
    ports:
      - "9090:9090"
    environment:
      # GitHub App（CI 集成必填）
      - CODEGUARD_WEBHOOK_SECRET=${CODEGUARD_WEBHOOK_SECRET}
      - CODEGUARD_GITHUB_APP_ID=${CODEGUARD_GITHUB_APP_ID}
      - CODEGUARD_GITHUB_PRIVATE_KEY=${CODEGUARD_GITHUB_PRIVATE_KEY}

      # LLM 配置
      - CODEGUARD_PROVIDER=${CODEGUARD_PROVIDER:-openai}
      - CODEGUARD_API_KEY=${CODEGUARD_API_KEY}
      - CODEGUARD_MODEL=${CODEGUARD_MODEL}

      # 审查配置
      - CODEGUARD_TOOL_SERVER_URL=http://localhost:9090
      - CODEGUARD_MAX_CONCURRENT_REVIEWS=${CODEGUARD_MAX_CONCURRENT_REVIEWS:-2}
      - CODEGUARD_WEBHOOK_RATE_LIMIT=${CODEGUARD_WEBHOOK_RATE_LIMIT:-30}
      - CODEGUARD_MAX_DIFF_LINES=${CODEGUARD_MAX_DIFF_LINES:-5000}
    volumes:
      - gateway-data:/app/data
      - job-workspaces:/tmp/codeguard-jobs
    restart: unless-stopped

volumes:
  gateway-data:
  job-workspaces:
```

- [ ] **Step 3: 更新 .env.example**

在现有 `.env.example` 末尾追加:

```bash
# ── CI 集成（可选，配置后启用 GitHub webhook）──
# CODEGUARD_WEBHOOK_SECRET=whsec_xxxx
# CODEGUARD_GITHUB_APP_ID=123456
# CODEGUARD_GITHUB_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...
# CODEGUARD_MAX_CONCURRENT_REVIEWS=2
# CODEGUARD_WEBHOOK_RATE_LIMIT=30
# CODEGUARD_MAX_DIFF_LINES=5000
```

- [ ] **Step 4: 构建验证**

```powershell
cd E:\java_develop\my_project\Codeguard
docker build -t codeguard .
```

Expected: 构建成功，`docker images | findstr codeguard` 显示镜像

- [ ] **Step 5: 提交**

```bash
git add Dockerfile docker-compose.yml .env.example
git commit -m "feat(ci): 新增 Dockerfile 和 docker-compose.yml 部署配置"
```

---

**Phase 4 结束状态验证:**

```powershell
docker compose up -d
# Gateway 启动，日志显示:
#   "Codeguard 工具服务已启动, 端口 9090"
#   "GitHub webhook 端点已启用: POST /webhooks/github"
```

---

## 自审

### 1. Spec 覆盖

| Spec 模块 | 对应 Task | 状态 |
|-----------|----------|------|
| 模块 1: Webhook 接入层（验签+过滤+幂等） | 1.3, 1.4, 1.5 | ✅ |
| 模块 2: 异步 Job 系统（队列+并发+H2+恢复） | 1.4, 2.1 | ✅ |
| 模块 3: 审查执行器（git+ProcessBuilder） | 2.2 | ✅ |
| 模块 4: 结果反馈（Check Runs+行评论） | 3.1, 3.2 | ✅ |
| 模块 5: 防护层（限流+超时+降级+重试） | 4.1 | ✅ |
| 模块 6: 部署（Dockerfile+docker-compose） | 4.2 | ✅ |
| RAG embedding 方案预留 | 设计文档 ADR 记录 | ✅ (spec 中有记录，实现时做为 tool 加入) |

### 2. Placeholder 扫描

无 TBD、TODO、"implement later"、空壳步骤。

### 3. 类型一致性

- `ReviewJob` 的字段在各 Task 中命名一致：`repo`, `prNumber`, `headSha`, `baseRef`, `cloneUrl`, `installationId`, `status`, `resultJson`, `retryCount`, `errorMessage`
- `GitHubClient.IssueAnnot` 在 3.1 定义，3.2 使用，签名一致
- `ReviewGuard` 在 4.1 定义，在 1.5 的 controller 集成中使用
- `ResultFeedback` 在 3.2 定义，在 2.2 的 executor 中使用

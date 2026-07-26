package com.codeguard.ci.model;

import java.time.Instant;
import java.util.Objects;

/**
 * 审查 job 实体。持久化到 MySQL，重启不丢。
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
    private String diffText;
    private Status status;
    private String resultJson;
    private int retryCount;
    private String errorMessage;
    private Instant createdAt;
    private Instant updatedAt;

    public ReviewJob() {
        this.status = Status.PENDING;
        this.retryCount = 0;
        this.createdAt = Instant.now();
        this.updatedAt = Instant.now();
    }

    /**
     * 供 JobRepository.mapRow() 使用，设置不可变业务键字段。
     * 不触发 updatedAt 变更。
     */
    public ReviewJob(String repo, int prNumber, String headSha, String baseRef, String cloneUrl) {
        this.repo = repo;
        this.prNumber = prNumber;
        this.headSha = headSha;
        this.baseRef = baseRef;
        this.cloneUrl = cloneUrl;
        this.status = Status.PENDING;
        this.retryCount = 0;
        this.createdAt = Instant.now();
        this.updatedAt = Instant.now();
    }

    public ReviewJob(WebhookPayload payload) {
        Objects.requireNonNull(payload, "WebhookPayload 不能为 null");
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
    public String getDiffText() { return diffText; }
    public Status getStatus() { return status; }
    public String getResultJson() { return resultJson; }
    public int getRetryCount() { return retryCount; }
    public String getErrorMessage() { return errorMessage; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }

    // setters — 所有 setter 统一更新 updatedAt
    public void setId(Long id) { this.id = id; this.updatedAt = Instant.now(); }
    public void setStatus(Status status) { this.status = status; this.updatedAt = Instant.now(); }
    public void setResultJson(String resultJson) { this.resultJson = resultJson; this.updatedAt = Instant.now(); }
    public void setRetryCount(int retryCount) { this.retryCount = retryCount; this.updatedAt = Instant.now(); }
    public void setErrorMessage(String errorMessage) { this.errorMessage = errorMessage; this.updatedAt = Instant.now(); }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
    public void setInstallationId(long installationId) { this.installationId = installationId; this.updatedAt = Instant.now(); }
    public void setDiffText(String diffText) { this.diffText = diffText; this.updatedAt = Instant.now(); }

    // ---- 供 JobRepository.mapRow() 使用，不触发 updatedAt 变更 ----
    public void setIdFromDb(Long id) { this.id = id; }
    public void setStatusFromDb(Status status) { this.status = status; }
    public void setResultJsonFromDb(String resultJson) { this.resultJson = resultJson; }
    public void setRetryCountFromDb(int retryCount) { this.retryCount = retryCount; }
    public void setErrorMessageFromDb(String errorMessage) { this.errorMessage = errorMessage; }
    public void setCreatedAtFromDb(Instant createdAt) { this.createdAt = createdAt; }
    public void setUpdatedAtFromDb(Instant updatedAt) { this.updatedAt = updatedAt; }
    public void setInstallationIdFromDb(long installationId) { this.installationId = installationId; }
    public void setDiffTextFromDb(String diffText) { this.diffText = diffText; }

    public String dedupKey() {
        return repo + ":" + prNumber + ":" + headSha;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof ReviewJob other)) return false;
        return Objects.equals(dedupKey(), other.dedupKey());
    }

    @Override
    public int hashCode() {
        return Objects.hash(dedupKey());
    }

    @Override
    public String toString() {
        return "ReviewJob{" + dedupKey() + " status=" + status + " retry=" + retryCount + "}";
    }
}

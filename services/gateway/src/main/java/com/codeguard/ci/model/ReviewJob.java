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

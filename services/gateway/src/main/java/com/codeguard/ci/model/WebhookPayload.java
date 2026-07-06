package com.codeguard.ci.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Objects;

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
    @JsonProperty("head_ref") String headRef,
    @JsonProperty("installation_id") long installationId
) {
    public WebhookPayload {
        Objects.requireNonNull(repoFullName, "repoFullName 不能为 null");
        Objects.requireNonNull(cloneUrl, "cloneUrl 不能为 null");
        Objects.requireNonNull(headSha, "headSha 不能为 null");
        Objects.requireNonNull(baseRef, "baseRef 不能为 null");
        Objects.requireNonNull(headRef, "headRef 不能为 null");
    }

    /** 幂等去重 key */
    public String dedupKey() {
        return repoFullName + ":" + prNumber + ":" + headSha;
    }
}

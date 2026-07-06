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

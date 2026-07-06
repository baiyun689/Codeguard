package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;

/**
 * 异步 job 调度器（暂存，P2.1 实现完整逻辑）。
 * <p>
 * 当前为最小存根，仅使 GitHubWebhookController 可编译。
 * submit() 始终返回 true。
 */
public class JobScheduler {

    private final JobRepository repo;
    private final int maxConcurrent;

    public JobScheduler(JobRepository repo, int maxConcurrent, Object unused) {
        this.repo = repo;
        this.maxConcurrent = maxConcurrent;
    }

    /**
     * 提交 job 到执行队列。
     * @param job 待执行的 ReviewJob
     * @return true 表示已入队（当前实现始终返回 true）
     */
    public boolean submit(ReviewJob job) {
        return true;
    }
}

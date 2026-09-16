package com.codeguard.ci.executor;

import com.codeguard.ci.model.ReviewJob;

@FunctionalInterface
public interface ReviewExecutor {
    ReviewExecutionOutcome execute(ReviewJob job);

    default void cleanup(ReviewJob job) {
        // 未分配工作区的执行器无需清理资源。
    }
}

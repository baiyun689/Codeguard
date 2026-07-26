package com.codeguard.ci.executor;

import com.codeguard.ci.model.ReviewJob;

@FunctionalInterface
public interface ReviewExecutor {
    ReviewExecutionOutcome execute(ReviewJob job);

    default void cleanup(ReviewJob job) {
        // Executors without a workspace have nothing to clean.
    }
}

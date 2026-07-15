package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;

@FunctionalInterface
public interface FeedbackPublisher {
    boolean publish(ReviewJob job);
}

package com.codeguard.ci.executor;

import java.time.Duration;

public sealed interface ReviewExecutionOutcome {
    Duration duration();

    record Succeeded(String resultJson, String diffText, int processExitCode,
                     Duration duration) implements ReviewExecutionOutcome {}

    record Failed(FailureCode code, String message, boolean retryable,
                  Duration duration) implements ReviewExecutionOutcome {}
}

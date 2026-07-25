package com.codeguard.ci.executor;

import java.time.Duration;

public record CommandResult(int exitCode, String stdout, String stderr,
                            boolean timedOut, Duration duration) {}

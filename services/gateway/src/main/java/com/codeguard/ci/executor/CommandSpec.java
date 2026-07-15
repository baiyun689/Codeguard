package com.codeguard.ci.executor;

import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;

public record CommandSpec(List<String> command, Path workingDirectory,
                          Map<String, String> environment, Duration timeout,
                          int stdoutLimitBytes, int stderrLimitBytes) {
    public CommandSpec {
        command = List.copyOf(command);
        environment = Map.copyOf(environment);
    }

    @Override
    public String toString() {
        return "CommandSpec[command=" + command + ", workingDirectory=" + workingDirectory
            + ", environmentKeys=" + environment.keySet() + ", timeout=" + timeout
            + ", stdoutLimitBytes=" + stdoutLimitBytes + ", stderrLimitBytes=" + stderrLimitBytes + "]";
    }
}

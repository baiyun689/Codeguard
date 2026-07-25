package com.codeguard.ci.executor;

import java.io.IOException;

@FunctionalInterface
public interface CommandRunner {
    CommandResult run(CommandSpec spec) throws IOException, InterruptedException;
}

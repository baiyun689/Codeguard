package com.codeguard.ci.executor;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

public final class ProcessCommandRunner implements CommandRunner {
    @Override
    public CommandResult run(CommandSpec spec) throws IOException, InterruptedException {
        Instant started = Instant.now();
        ProcessBuilder builder = new ProcessBuilder(spec.command());
        builder.directory(spec.workingDirectory().toFile());
        builder.environment().putAll(spec.environment());
        Process process = builder.start();

        CompletableFuture<String> stdout = drain(process.getInputStream(), spec.stdoutLimitBytes());
        CompletableFuture<String> stderr = drain(process.getErrorStream(), spec.stderrLimitBytes());
        boolean finished;
        try {
            finished = process.waitFor(spec.timeout().toMillis(), TimeUnit.MILLISECONDS);
        } catch (InterruptedException interrupted) {
            killProcessTree(process);
            Thread.currentThread().interrupt();
            throw interrupted;
        }
        if (!finished) killProcessTree(process);

        return new CommandResult(
            finished ? process.exitValue() : -1,
            stdout.join(), stderr.join(), !finished,
            Duration.between(started, Instant.now()));
    }

    private static CompletableFuture<String> drain(InputStream stream, int limit) {
        return CompletableFuture.supplyAsync(() -> {
            try (stream; ByteArrayOutputStream kept = new ByteArrayOutputStream(Math.min(limit, 8192))) {
                byte[] buffer = new byte[8192];
                int read;
                int remaining = limit;
                while ((read = stream.read(buffer)) != -1) {
                    int copy = Math.min(read, Math.max(remaining, 0));
                    if (copy > 0) kept.write(buffer, 0, copy);
                    remaining -= copy;
                }
                return kept.toString(StandardCharsets.UTF_8);
            } catch (IOException ignored) {
                return "";
            }
        });
    }

    static void killProcessTree(Process process) {
        var descendants = new ArrayList<>(process.toHandle().descendants().toList());
        descendants.sort(Comparator.comparingLong(ProcessHandle::pid).reversed());
        descendants.forEach(ProcessHandle::destroyForcibly);
        process.destroyForcibly();
        try {
            process.waitFor(5, TimeUnit.SECONDS);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }
}

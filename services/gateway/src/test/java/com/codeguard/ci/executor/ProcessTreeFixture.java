package com.codeguard.ci.executor;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

public final class ProcessTreeFixture {
    private ProcessTreeFixture() {}

    public static void main(String[] args) throws Exception {
        if ("output".equals(args[0])) {
            System.out.print("x".repeat(32_000));
            System.err.print("y".repeat(32_000));
            return;
        }
        Path pids = Path.of(args[1]);
        Files.writeString(pids, ProcessHandle.current().pid() + System.lineSeparator(),
            StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        if ("parent".equals(args[0])) {
            new ProcessBuilder(javaExecutable(), "-cp", System.getProperty("java.class.path"),
                ProcessTreeFixture.class.getName(), "child", pids.toString()).start();
        }
        Thread.sleep(60_000);
    }

    private static String javaExecutable() {
        return Path.of(System.getProperty("java.home"), "bin", "java").toString();
    }
}

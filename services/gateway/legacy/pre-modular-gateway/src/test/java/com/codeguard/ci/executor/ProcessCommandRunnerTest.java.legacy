package com.codeguard.ci.executor;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;

class ProcessCommandRunnerTest {
    @TempDir Path temp;

    @Test
    void timeoutKillsParentAndDescendantProcesses() throws Exception {
        Path pids = temp.resolve("pids.txt");
        CommandSpec spec = new CommandSpec(List.of(javaExecutable(), "-cp",
            System.getProperty("java.class.path"), ProcessTreeFixture.class.getName(),
            "parent", pids.toString()), temp, Map.of(), Duration.ofSeconds(2), 1024, 1024);

        CommandResult result = new ProcessCommandRunner().run(spec);

        assertTrue(result.timedOut());
        List<Long> recorded = Files.readAllLines(pids).stream().map(Long::parseLong).toList();
        assertEquals(2, recorded.size());
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (System.nanoTime() < deadline && recorded.stream().anyMatch(ProcessCommandRunnerTest::alive)) {
            Thread.onSpinWait();
        }
        assertTrue(recorded.stream().noneMatch(ProcessCommandRunnerTest::alive), "parent and child must be dead");
    }

    @Test
    void boundsCapturedOutputWhileStillDrainingBothStreams() throws Exception {
        CommandSpec spec = new CommandSpec(List.of(javaExecutable(), "-cp",
            System.getProperty("java.class.path"), ProcessTreeFixture.class.getName(), "output"),
            temp, Map.of(), Duration.ofSeconds(5), 1024, 512);

        CommandResult result = new ProcessCommandRunner().run(spec);

        assertEquals(0, result.exitCode());
        assertEquals(1024, result.stdout().length());
        assertEquals(512, result.stderr().length());
    }

    private static boolean alive(long pid) {
        return ProcessHandle.of(pid).map(ProcessHandle::isAlive).orElse(false);
    }

    private static String javaExecutable() {
        return Path.of(System.getProperty("java.home"), "bin", "java").toString();
    }
}

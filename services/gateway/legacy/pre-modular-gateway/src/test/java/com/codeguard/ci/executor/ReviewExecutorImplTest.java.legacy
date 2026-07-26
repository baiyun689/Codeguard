package com.codeguard.ci.executor;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class ReviewExecutorImplTest {

    private static final String VALID = "{\"issues\":[],\"summary\":\"clean\"}";

    @TempDir
    Path workspaceRoot;

    @Test
    void acceptsValidReviewJsonWithExitZero() {
        FakeCommandRunner runner = successfulRunner(0, VALID);
        ReviewExecutionOutcome outcome = executor(runner).execute(job("sha-zero"));

        var succeeded = assertInstanceOf(ReviewExecutionOutcome.Succeeded.class, outcome);
        assertEquals(0, succeeded.processExitCode());
        assertEquals(VALID, succeeded.resultJson());
        assertEquals("diff", succeeded.diffText());
    }

    @Test
    void acceptsValidReviewJsonWithExitOneBecauseCriticalIsAProductResult() {
        String critical = "{\"issues\":[{\"severity\":\"CRITICAL\"}],\"summary\":\"blocked\"}";
        ReviewExecutionOutcome outcome = executor(successfulRunner(1, critical)).execute(job("sha-critical"));

        var succeeded = assertInstanceOf(ReviewExecutionOutcome.Succeeded.class, outcome);
        assertEquals(1, succeeded.processExitCode());
        assertEquals(critical, succeeded.resultJson());
    }

    @Test
    void rejectsExitOneWhenStdoutIsNotReviewResultJson() {
        ReviewExecutionOutcome outcome = executor(successfulRunner(1, "not-json")).execute(job("sha-bad"));

        var failed = assertInstanceOf(ReviewExecutionOutcome.Failed.class, outcome);
        assertEquals(FailureCode.INVALID_REVIEW_OUTPUT, failed.code());
        assertFalse(failed.retryable());
    }

    @Test
    void rejectsJsonMissingReviewResultContractFields() {
        ReviewExecutionOutcome outcome = executor(successfulRunner(0, "{\"issues\":[]}")).execute(job("sha-missing"));

        var failed = assertInstanceOf(ReviewExecutionOutcome.Failed.class, outcome);
        assertEquals(FailureCode.INVALID_REVIEW_OUTPUT, failed.code());
    }

    @Test
    void usesFullShaToIsolateWorkspaces() {
        FakeCommandRunner runner = new FakeCommandRunner();
        ReviewExecutorImpl executor = executor(runner);

        Path first = executor.workspaceFor(job("0123456789abcdef"));
        Path second = executor.workspaceFor(job("0123456789abcdee"));

        assertNotEquals(first, second);
        assertTrue(first.toString().endsWith("0123456789abcdef"));
    }

    @Test
    void passesTokenOnlyThroughGitEnvironmentAndNeverCommandArguments() {
        FakeCommandRunner runner = successfulRunner(0, VALID);
        executor(runner).execute(job("sha-secret"));

        for (CommandSpec command : runner.commands) {
            assertFalse(String.join(" ", command.command()).contains("very-secret-token"));
            assertFalse(command.toString().contains("very-secret-token"));
        }
        assertTrue(runner.commands.stream()
            .filter(c -> c.command().contains("git"))
            .anyMatch(c -> c.environment().getOrDefault("GIT_CONFIG_VALUE_0", "")
                .startsWith("AUTHORIZATION: basic ")));
        assertTrue(runner.commands.stream()
            .flatMap(c -> c.environment().values().stream())
            .noneMatch(v -> v.contains("very-secret-token")));
        assertFalse(SecretRedactor.redact("failed very-secret-token", "very-secret-token")
            .contains("very-secret-token"));
    }

    private ReviewExecutorImpl executor(FakeCommandRunner runner) {
        return new ReviewExecutorImpl(
            workspaceRoot, "very-secret-token", Duration.ofSeconds(5), runner);
    }

    private static FakeCommandRunner successfulRunner(int reviewExit, String reviewStdout) {
        FakeCommandRunner runner = new FakeCommandRunner();
        runner.results.add(ok("")); // clone
        runner.results.add(ok("")); // fetch base
        runner.results.add(ok("")); // fetch PR
        runner.results.add(ok("")); // checkout
        runner.results.add(ok("diff"));
        runner.results.add(new CommandResult(reviewExit, reviewStdout, "diagnostic", false, Duration.ofMillis(5)));
        return runner;
    }

    private static CommandResult ok(String stdout) {
        return new CommandResult(0, stdout, "", false, Duration.ofMillis(1));
    }

    private static ReviewJob job(String sha) {
        return new ReviewJob(new WebhookPayload(
            "acme/demo", "https://github.com/acme/demo.git", 42, sha, "main", "feature", 7L));
    }

    private static final class FakeCommandRunner implements CommandRunner {
        private final ArrayDeque<CommandResult> results = new ArrayDeque<>();
        private final List<CommandSpec> commands = new ArrayList<>();

        @Override
        public CommandResult run(CommandSpec spec) {
            commands.add(spec);
            return results.removeFirst();
        }
    }
}

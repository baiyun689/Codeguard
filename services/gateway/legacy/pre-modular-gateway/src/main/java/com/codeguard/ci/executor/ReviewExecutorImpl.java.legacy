package com.codeguard.ci.executor;

import com.codeguard.ci.model.ReviewJob;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Executes one isolated review attempt. Persistence, retry and feedback belong to JobScheduler. */
public final class ReviewExecutorImpl implements ReviewExecutor {
    private static final Logger log = LoggerFactory.getLogger(ReviewExecutorImpl.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int STDOUT_LIMIT = 10 * 1024 * 1024;
    private static final int STDERR_LIMIT = 64 * 1024;

    private final Path workspaceRoot;
    private final String githubToken;
    private final Duration reviewTimeout;
    private final CommandRunner runner;
    private final String pythonCommand;

    public ReviewExecutorImpl(Path workspaceRoot, String githubToken, Duration reviewTimeout) {
        this(workspaceRoot, githubToken, reviewTimeout,
            System.getenv().getOrDefault("CODEGUARD_PYTHON", "python"), new ProcessCommandRunner());
    }

    ReviewExecutorImpl(Path workspaceRoot, String githubToken, Duration reviewTimeout, CommandRunner runner) {
        this(workspaceRoot, githubToken, reviewTimeout,
            System.getenv().getOrDefault("CODEGUARD_PYTHON", "python"), runner);
    }

    public ReviewExecutorImpl(Path workspaceRoot, String githubToken, Duration reviewTimeout,
                              String pythonCommand) {
        this(workspaceRoot, githubToken, reviewTimeout, pythonCommand, new ProcessCommandRunner());
    }

    ReviewExecutorImpl(Path workspaceRoot, String githubToken, Duration reviewTimeout,
                       String pythonCommand, CommandRunner runner) {
        this.workspaceRoot = workspaceRoot;
        this.githubToken = githubToken == null ? "" : githubToken;
        this.reviewTimeout = reviewTimeout;
        this.pythonCommand = pythonCommand;
        this.runner = runner;
    }

    @Override
    public ReviewExecutionOutcome execute(ReviewJob job) {
        Instant started = Instant.now();
        Path workspace = workspaceFor(job);
        try {
            prepareWorkspace(job, workspace);
            CommandResult diff = run(gitCommand(workspace,
                "git", "diff", "origin/" + job.getBaseRef() + "..." + job.getHeadSha()), false);
            if (diff.timedOut() || diff.exitCode() != 0) {
                return failed(FailureCode.GIT_COMMAND_FAILED, diff, true, started);
            }

            CommandResult review = run(reviewCommand(workspace, job), true);
            if (review.timedOut()) {
                return new ReviewExecutionOutcome.Failed(FailureCode.PROCESS_TIMEOUT,
                    "Python 审查进程超时", true, elapsed(started));
            }
            if ((review.exitCode() == 0 || review.exitCode() == 1) && validReviewJson(review.stdout())) {
                deleteWorkspace(workspace);
                return new ReviewExecutionOutcome.Succeeded(review.stdout().trim(), diff.stdout(),
                    review.exitCode(), elapsed(started));
            }
            if (!validReviewJson(review.stdout())) {
                deleteWorkspace(workspace);
                return new ReviewExecutionOutcome.Failed(FailureCode.INVALID_REVIEW_OUTPUT,
                    "Python 输出不符合 ReviewResult JSON 契约", false, elapsed(started));
            }
            return failed(FailureCode.REVIEW_PROCESS_FAILED, review, true, started);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            return new ReviewExecutionOutcome.Failed(FailureCode.INTERRUPTED,
                "审查被中断", true, elapsed(started));
        } catch (GitCommandException git) {
            return new ReviewExecutionOutcome.Failed(git.code,
                SecretRedactor.redact(git.getMessage(), githubToken), true, elapsed(started));
        } catch (IOException io) {
            return new ReviewExecutionOutcome.Failed(FailureCode.IO_ERROR,
                SecretRedactor.redact(io.getMessage(), githubToken), true, elapsed(started));
        } catch (RuntimeException unexpected) {
            deleteWorkspace(workspace);
            return new ReviewExecutionOutcome.Failed(FailureCode.IO_ERROR,
                SecretRedactor.redact(unexpected.getMessage(), githubToken), false, elapsed(started));
        }
    }

    @Override
    public void cleanup(ReviewJob job) {
        deleteWorkspace(workspaceFor(job));
    }

    Path workspaceFor(ReviewJob job) {
        String repo = job.getRepo().replace('/', '_').replaceAll("[^a-zA-Z0-9_.-]", "");
        String sha = job.getHeadSha().replaceAll("[^a-fA-F0-9]", "");
        if (sha.isBlank()) sha = job.getHeadSha().replaceAll("[^a-zA-Z0-9_.-]", "");
        return workspaceRoot.resolve(repo).resolve("pr-" + job.getPrNumber()).resolve(sha);
    }

    private void prepareWorkspace(ReviewJob job, Path workspace) throws IOException, InterruptedException {
        Files.createDirectories(workspace.getParent());
        if (!Files.exists(workspace.resolve(".git"))) {
            requireGitSuccess(run(gitCommand(workspace.getParent(),
                "git", "clone", "--depth=50", job.getCloneUrl(), workspace.getFileName().toString()), false));
        }
        requireGitSuccess(run(gitCommand(workspace, "git", "fetch", "origin",
            job.getBaseRef() + ":refs/remotes/origin/" + job.getBaseRef()), false));
        requireGitSuccess(run(gitCommand(workspace, "git", "fetch", "origin",
            "pull/" + job.getPrNumber() + "/head"), false));
        requireGitSuccess(run(gitCommand(workspace, "git", "checkout", "--detach", job.getHeadSha()), false));
    }

    private void requireGitSuccess(CommandResult result) throws GitCommandException {
        if (result.timedOut()) throw new GitCommandException(FailureCode.PROCESS_TIMEOUT, "git 命令超时");
        if (result.exitCode() != 0) {
            throw new GitCommandException(FailureCode.GIT_COMMAND_FAILED,
                "git 命令失败: " + SecretRedactor.redact(result.stderr(), githubToken));
        }
    }

    private CommandSpec gitCommand(Path directory, String... command) {
        Map<String, String> env = new HashMap<>();
        if (!githubToken.isBlank()) {
            String credentials = Base64.getEncoder().encodeToString(
                ("x-access-token:" + githubToken).getBytes(StandardCharsets.UTF_8));
            env.put("GIT_CONFIG_COUNT", "1");
            env.put("GIT_CONFIG_KEY_0", "http.https://github.com/.extraheader");
            env.put("GIT_CONFIG_VALUE_0", "AUTHORIZATION: basic " + credentials);
        }
        return new CommandSpec(List.of(command), directory, env, Duration.ofMinutes(2),
            STDOUT_LIMIT, STDERR_LIMIT);
    }

    private CommandSpec reviewCommand(Path workspace, ReviewJob job) {
        List<String> command = new ArrayList<>(List.of(pythonCommand, "-m", "codeguard_agent", "review",
            "--repo", workspace.toString(), "--base", "origin/" + job.getBaseRef(), "--format", "json"));
        Map<String, String> env = new HashMap<>();
        System.getenv().forEach((key, value) -> {
            if (key.startsWith("CODEGUARD_")) env.put(key, value);
        });
        env.putIfAbsent("CODEGUARD_TOOL_SERVER_URL", "http://localhost:9090");
        return new CommandSpec(command, workspace, env, reviewTimeout, STDOUT_LIMIT, STDERR_LIMIT);
    }

    private CommandResult run(CommandSpec spec, boolean review) throws IOException, InterruptedException {
        CommandResult result = runner.run(spec);
        if (!result.stderr().isBlank()) {
            log.warn("{} stderr: {}", review ? "review" : "git",
                SecretRedactor.redact(result.stderr(), githubToken));
        }
        return result;
    }

    private static boolean validReviewJson(String output) {
        if (output == null || output.isBlank()) return false;
        try {
            JsonNode root = MAPPER.readTree(output);
            return root.isObject() && root.path("issues").isArray()
                && root.has("summary") && root.path("summary").isTextual();
        } catch (Exception ignored) {
            return false;
        }
    }

    private ReviewExecutionOutcome.Failed failed(FailureCode code, CommandResult result,
                                                   boolean retryable, Instant started) {
        String message = result.stderr().isBlank() ? code.name() : result.stderr();
        return new ReviewExecutionOutcome.Failed(code,
            SecretRedactor.redact(message, githubToken), retryable, elapsed(started));
    }

    private static Duration elapsed(Instant started) {
        return Duration.between(started, Instant.now());
    }

    private static void deleteWorkspace(Path workspace) {
        if (!Files.exists(workspace)) return;
        try (var paths = Files.walk(workspace)) {
            paths.sorted((left, right) -> right.compareTo(left)).forEach(path -> {
                try { Files.deleteIfExists(path); } catch (IOException ignored) { }
            });
        } catch (IOException ignored) {
            // Best-effort cleanup; stale SHA-scoped workspaces cannot corrupt another review.
        }
    }

    private static final class GitCommandException extends IOException {
        private final FailureCode code;

        private GitCommandException(FailureCode code, String message) {
            super(message);
            this.code = code;
        }
    }
}

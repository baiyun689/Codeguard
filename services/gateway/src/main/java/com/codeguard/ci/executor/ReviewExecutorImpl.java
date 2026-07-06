package com.codeguard.ci.executor;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.TimeUnit;

/**
 * 审查执行器: git clone/fetch + ProcessBuilder 调 Python CLI + 结果解析 + 重试。
 */
public class ReviewExecutorImpl {

    private static final Logger log = LoggerFactory.getLogger(ReviewExecutorImpl.class);

    private final JobRepository repo;
    private final Path workspacesDir;
    private final String githubToken;
    private final ResultFeedback feedback;

    public ReviewExecutorImpl(JobRepository repo, Path workspacesDir, String githubToken) {
        this(repo, workspacesDir, githubToken, null);
    }

    public ReviewExecutorImpl(JobRepository repo, Path workspacesDir, String githubToken,
                              ResultFeedback feedback) {
        this.repo = repo;
        this.workspacesDir = workspacesDir;
        this.githubToken = githubToken;
        this.feedback = feedback;
    }

    /**
     * 执行审查 job。供 JobScheduler 通过 Consumer&lt;ReviewJob&gt; 回调。
     * 内部处理 git clone/fetch、Python CLI 调用、结果解析、重试。
     */
    public void accept(ReviewJob job) {
        Path workdir = null;
        try {
            workdir = cloneOrFetch(job);
            List<String> cmd = buildCommand(workdir, job);
            String stdout = runProcess(cmd, workdir);

            if (stdout == null || stdout.isBlank()) {
                handleFailure(job, true, "审查输出为空");
                return;
            }

            job.setResultJson(stdout);
            job.setStatus(Status.DONE);
            repo.update(job);
            if (feedback != null) {
                feedback.postResults(job);
            }
            log.info("审查完成: {} PR#{}", job.getRepo(), job.getPrNumber());

        } catch (IOException e) {
            log.error("审查执行失败(IO): {}", job.dedupKey(), e);
            handleFailure(job, false, e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            handleFailure(job, true, "审查被中断");
        } catch (ProcessTimeoutException e) {
            log.error("审查超时: {}", job.dedupKey());
            handleFailure(job, true, "审查进程超时 (10min)");
        } catch (Exception e) {
            log.error("审查执行失败: {}", job.dedupKey(), e);
            handleFailure(job, isTransient(e), e.getMessage());
        }
    }

    // ── git clone/fetch ──

    Path cloneOrFetch(ReviewJob job) throws IOException, InterruptedException {
        String safeName = sanitizeDirName(job.getRepo()) + "-pr-" + job.getPrNumber();
        Path dir = workspacesDir.resolve(safeName);
        String cloneUrl = job.getCloneUrl();

        if (githubToken != null && !githubToken.isBlank() && cloneUrl.startsWith("https://")) {
            cloneUrl = cloneUrl.replace("https://", "https://x-access-token:" + githubToken + "@");
        }

        if (Files.exists(dir.resolve(".git"))) {
            log.info("fetch 已有仓库: {}", dir);
            runCmd(dir, 2, TimeUnit.MINUTES, "git", "fetch", "origin", job.getBaseRef());
        } else {
            log.info("clone 新仓库: {} → {}", cloneUrl, dir);
            Files.createDirectories(dir.getParent());
            runCmd(dir.getParent(), 2, TimeUnit.MINUTES,
                "git", "clone", "--depth=50", "--branch", job.getBaseRef(), cloneUrl, dir.getFileName().toString());
        }
        return dir;
    }

    private String sanitizeDirName(String repo) {
        return repo.replace('/', '_').replaceAll("[^a-zA-Z0-9_.-]", "");
    }

    // ── Python CLI ──

    List<String> buildCommand(Path workdir, ReviewJob job) {
        List<String> cmd = new ArrayList<>(List.of(
            "python", "-m", "codeguard_agent", "review",
            "--repo", workdir.toString(),
            "--base", "origin/" + job.getBaseRef(),
            "--mode", "pipeline",
            "--format", "json"
        ));
        return cmd;
    }

    String runProcess(List<String> cmd, Path workdir)
            throws IOException, InterruptedException, ProcessTimeoutException {
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.directory(workdir.toFile());
        pb.redirectErrorStream(false);

        Map<String, String> env = pb.environment();
        System.getenv().forEach((k, v) -> {
            if (k.startsWith("CODEGUARD_")) env.put(k, v);
        });
        env.putIfAbsent("CODEGUARD_TOOL_SERVER_URL", "http://localhost:9090");

        Process process = pb.start();
        String stdout = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        String stderr = new String(process.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);

        boolean finished = process.waitFor(10, TimeUnit.MINUTES);
        if (!finished) {
            process.destroyForcibly();
            throw new ProcessTimeoutException();
        }

        if (!stderr.isBlank()) {
            int maxLen = Math.min(500, stderr.length());
            log.warn("审查 stderr(前{}字符): {}", maxLen, stderr.substring(0, maxLen));
        }

        return stdout.trim();
    }

    // ── 重试逻辑 ──

    void handleFailure(ReviewJob job, boolean retryable, String message) {
        job.setErrorMessage(message != null ? message : "未知错误");
        if (retryable && job.getRetryCount() < 2) {
            job.setStatus(Status.RETRYING);
            job.setRetryCount(job.getRetryCount() + 1);
            repo.update(job);
            log.info("审查失败，30s 后重试 (第{}次): {}", job.getRetryCount(), job.dedupKey());
            // 30s sleep then retry synchronously
            try { Thread.sleep(30_000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
            accept(job);
        } else {
            job.setStatus(Status.FAILED);
            repo.update(job);
            log.error("审查最终失败(重试{}次): {}", job.getRetryCount(), job.dedupKey());
        }
    }

    private boolean isTransient(Exception e) {
        String msg = e.getMessage();
        if (msg == null) return false;
        String lower = msg.toLowerCase();
        return lower.contains("timeout") || lower.contains("connection") || lower.contains("transient");
    }

    // ── helpers ──

    private void runCmd(Path dir, long timeout, TimeUnit unit, String... args)
            throws IOException, InterruptedException {
        ProcessBuilder pb = new ProcessBuilder(args);
        pb.directory(dir.toFile());
        pb.redirectErrorStream(true);
        Process p = pb.start();
        boolean ok = p.waitFor(timeout, unit);
        if (!ok) {
            p.destroyForcibly();
            throw new IOException("git 命令超时: " + String.join(" ", args));
        }
        if (p.exitValue() != 0) {
            String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            throw new IOException("git 命令失败: " + out);
        }
    }

    static class ProcessTimeoutException extends Exception {
        ProcessTimeoutException() { super("审查进程超时"); }
    }
}

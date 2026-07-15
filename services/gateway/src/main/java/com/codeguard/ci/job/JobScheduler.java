package com.codeguard.ci.job;

import com.codeguard.ci.executor.ReviewExecutionOutcome;
import com.codeguard.ci.executor.ReviewExecutor;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import com.codeguard.toolserver.GatewayMetrics;

import java.time.Duration;
import java.util.Set;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Single-instance scheduler that owns job state, retry, feedback and shutdown. */
public final class JobScheduler implements AutoCloseable {
    private static final Logger log = LoggerFactory.getLogger(JobScheduler.class);
    private static final int MAX_RETRIES = 2;

    private final JobRepository repository;
    private final ReviewExecutor executor;
    private final FeedbackPublisher feedback;
    private final Duration retryDelay;
    private final Duration shutdownGrace;
    private final ThreadPoolExecutor workers;
    private final ScheduledExecutorService retryTimer;
    private final Set<String> inFlight = ConcurrentHashMap.newKeySet();
    private final AtomicBoolean running = new AtomicBoolean();
    private final AtomicBoolean accepting = new AtomicBoolean();
    private final GatewayMetrics metrics;

    public JobScheduler(JobRepository repository, int maxConcurrency, ReviewExecutor executor,
                        Duration retryDelay, Duration shutdownGrace, FeedbackPublisher feedback) {
        this(repository, maxConcurrency, executor, retryDelay, shutdownGrace, feedback, new GatewayMetrics());
    }

    public JobScheduler(JobRepository repository, int maxConcurrency, ReviewExecutor executor,
                        Duration retryDelay, Duration shutdownGrace, FeedbackPublisher feedback,
                        GatewayMetrics metrics) {
        if (maxConcurrency < 1) throw new IllegalArgumentException("maxConcurrency 必须 >= 1");
        this.repository = repository;
        this.executor = executor;
        this.retryDelay = retryDelay;
        this.shutdownGrace = shutdownGrace;
        this.feedback = feedback == null ? ignored -> true : feedback;
        this.metrics = metrics;
        this.workers = new ThreadPoolExecutor(maxConcurrency, maxConcurrency, 0, TimeUnit.MILLISECONDS,
            new LinkedBlockingQueue<>(), new ThreadPoolExecutor.AbortPolicy());
        this.retryTimer = Executors.newSingleThreadScheduledExecutor();
    }

    public void start() {
        if (!running.compareAndSet(false, true)) return;
        accepting.set(true);
        var unfinished = repository.findUnfinished();
        log.info("启动恢复: 发现 {} 个未完成 job", unfinished.size());
        for (ReviewJob job : unfinished) {
            job.setStatus(Status.PENDING);
            repository.update(job);
            submit(job);
        }
    }

    public boolean submit(ReviewJob job) {
        if (!accepting.get() || !inFlight.add(job.dedupKey())) return false;
        if (enqueue(job)) return true;
        inFlight.remove(job.dedupKey());
        return false;
    }

    private boolean enqueue(ReviewJob job) {
        try {
            workers.execute(() -> executeJob(job));
            return true;
        } catch (RejectedExecutionException rejected) {
            log.warn("job 队列满或调度器关闭，拒绝: {}", job.dedupKey());
            return false;
        }
    }

    private void executeJob(ReviewJob job) {
        boolean terminal = false;
        try {
            job.setStatus(Status.RUNNING);
            repository.update(job);
            log.info("job={} repo={} pr={} sha={} event=review_started",
                job.getId(), job.getRepo(), job.getPrNumber(), shortSha(job.getHeadSha()));
            metrics.reviewStarted();
            ReviewExecutionOutcome outcome = executor.execute(job);
            if (outcome instanceof ReviewExecutionOutcome.Succeeded succeeded) {
                metrics.reviewSucceeded(succeeded.duration().toNanos() / 1_000_000_000.0);
                job.setResultJson(succeeded.resultJson());
                job.setDiffText(succeeded.diffText());
                job.setErrorMessage(null);
                job.setStatus(Status.DONE);
                repository.update(job);
                terminal = true;
                log.info("job={} repo={} pr={} sha={} event=review_completed exit_code={} duration_ms={}",
                    job.getId(), job.getRepo(), job.getPrNumber(), shortSha(job.getHeadSha()),
                    succeeded.processExitCode(), succeeded.duration().toMillis());
                try {
                    if (!feedback.publish(job)) {
                        metrics.feedbackFailure();
                        log.error("job={} event=feedback_failed", job.getId());
                    }
                } catch (RuntimeException feedbackFailure) {
                    metrics.feedbackFailure();
                    log.error("job={} event=feedback_failed message={}", job.getId(), feedbackFailure.getMessage());
                }
            } else if (outcome instanceof ReviewExecutionOutcome.Failed failed) {
                metrics.reviewFailed(failed.code().name(), failed.duration().toNanos() / 1_000_000_000.0);
                if (failed.code() == com.codeguard.ci.executor.FailureCode.PROCESS_TIMEOUT) {
                    metrics.processTimeout(failed.message().startsWith("git") ? "git" : "review");
                }
                job.setErrorMessage(failed.code() + ": " + failed.message());
                if (!running.get() && failed.code() == com.codeguard.ci.executor.FailureCode.INTERRUPTED) {
                    job.setStatus(Status.PENDING);
                    repository.update(job);
                    log.info("job={} event=review_interrupted_recoverable", job.getId());
                } else if (failed.retryable() && job.getRetryCount() < MAX_RETRIES && accepting.get()) {
                    job.setRetryCount(job.getRetryCount() + 1);
                    job.setStatus(Status.RETRYING);
                    repository.update(job);
                    scheduleRetry(job, failed.code().name());
                } else {
                    job.setStatus(Status.FAILED);
                    repository.update(job);
                    executor.cleanup(job);
                    terminal = true;
                    log.error("job={} repo={} pr={} sha={} event=review_failed reason={} retries={}",
                        job.getId(), job.getRepo(), job.getPrNumber(), shortSha(job.getHeadSha()),
                        failed.code(), job.getRetryCount());
                }
            }
        } catch (RuntimeException unexpected) {
            log.error("job={} event=scheduler_failure", job.getId(), unexpected);
            job.setErrorMessage(unexpected.getMessage());
            job.setStatus(running.get() ? Status.FAILED : Status.PENDING);
            try { repository.update(job); } catch (RuntimeException ignored) { }
            if (running.get()) executor.cleanup(job);
            terminal = running.get();
        } finally {
            metrics.reviewFinished();
            if (terminal || !accepting.get()) inFlight.remove(job.dedupKey());
        }
    }

    private void scheduleRetry(ReviewJob job, String reason) {
        log.info("job={} event=retry_scheduled reason={} attempt={} delay_seconds={}",
            job.getId(), reason, job.getRetryCount(), retryDelay.toSeconds());
        metrics.retry(reason);
        try {
            retryTimer.schedule(() -> {
                if (!accepting.get() || !enqueue(job)) {
                    job.setStatus(Status.PENDING);
                    repository.update(job);
                    inFlight.remove(job.dedupKey());
                }
            }, retryDelay.toMillis(), TimeUnit.MILLISECONDS);
        } catch (RejectedExecutionException rejected) {
            job.setStatus(Status.PENDING);
            repository.update(job);
            inFlight.remove(job.dedupKey());
        }
    }

    public boolean isReady() {
        return running.get() && accepting.get() && !workers.isShutdown() && repository.ping();
    }

    public int activeCount() {
        return workers.getActiveCount();
    }

    @Override
    public void close() {
        if (!running.getAndSet(false) && workers.isShutdown()) return;
        accepting.set(false);
        retryTimer.shutdownNow();
        workers.shutdown();
        try {
            if (!workers.awaitTermination(shutdownGrace.toMillis(), TimeUnit.MILLISECONDS)) {
                workers.shutdownNow();
                workers.awaitTermination(Math.min(shutdownGrace.toMillis(), 5_000), TimeUnit.MILLISECONDS);
            }
        } catch (InterruptedException interrupted) {
            workers.shutdownNow();
            Thread.currentThread().interrupt();
        }
        inFlight.clear();
        repository.close();
    }

    private static String shortSha(String sha) {
        if (sha == null) return "?";
        return sha.substring(0, Math.min(7, sha.length()));
    }
}

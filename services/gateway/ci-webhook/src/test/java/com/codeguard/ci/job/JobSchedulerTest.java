package com.codeguard.ci.job;

import com.codeguard.ci.executor.FailureCode;
import com.codeguard.ci.executor.ReviewExecutionOutcome;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class JobSchedulerTest {
    private JobRepository repo;
    private JobScheduler scheduler;
    private String dbPath;

    @BeforeEach
    void setUp() {
        dbPath = System.getProperty("java.io.tmpdir") + "/codeguard-sched-" + System.nanoTime();
        repo = new JobRepository(dbPath);
    }

    @AfterEach
    void tearDown() {
        if (scheduler != null) scheduler.close();
        repo.close();
    }

    @Test
    void persistsSuccessfulOutcome() throws Exception {
        CountDownLatch completed = new CountDownLatch(1);
        scheduler = scheduler(job -> {
            completed.countDown();
            return success();
        }, Duration.ofMillis(10), ignored -> true);
        scheduler.start();
        ReviewJob job = insert("sha1");

        assertTrue(scheduler.submit(job));
        assertTrue(completed.await(2, TimeUnit.SECONDS));
        awaitStatus(job, ReviewJob.Status.DONE);

        ReviewJob stored = repo.findByDedupKey("a/b", 1, "sha1").orElseThrow();
        assertEquals("{\"issues\":[],\"summary\":\"ok\"}", stored.getResultJson());
        assertEquals("diff", stored.getDiffText());
    }

    @Test
    void retryDelayDoesNotOccupyOnlyWorker() throws Exception {
        AtomicInteger firstAttempts = new AtomicInteger();
        CountDownLatch secondCompleted = new CountDownLatch(1);
        scheduler = scheduler(job -> {
            if (job.getHeadSha().equals("retry")) {
                if (firstAttempts.getAndIncrement() == 0) {
                    return new ReviewExecutionOutcome.Failed(
                        FailureCode.PROCESS_TIMEOUT, "timeout", true, Duration.ZERO);
                }
                return success();
            }
            secondCompleted.countDown();
            return success();
        }, Duration.ofSeconds(2), ignored -> true);
        scheduler.start();

        assertTrue(scheduler.submit(insert("retry")));
        awaitStatus(repo.findByDedupKey("a/b", 1, "retry").orElseThrow(), ReviewJob.Status.RETRYING);
        assertTrue(scheduler.submit(insert("other")));

        assertTrue(secondCompleted.await(500, TimeUnit.MILLISECONDS),
            "retry delay must not sleep on the only review worker");
    }

    @Test
    void duplicateSubmitExecutesSameJobOnlyOnce() throws Exception {
        AtomicInteger executions = new AtomicInteger();
        CountDownLatch entered = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        scheduler = scheduler(job -> {
            executions.incrementAndGet();
            entered.countDown();
            try { release.await(); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            return success();
        }, Duration.ofMillis(10), ignored -> true);
        scheduler.start();
        ReviewJob job = insert("same");

        assertTrue(scheduler.submit(job));
        assertTrue(entered.await(1, TimeUnit.SECONDS));
        assertFalse(scheduler.submit(job));
        release.countDown();

        awaitStatus(job, ReviewJob.Status.DONE);
        assertEquals(1, executions.get());
    }

    @Test
    void recoversRunningAndRetryingJobsOnStart() throws Exception {
        ReviewJob running = insert("running");
        running.setStatus(ReviewJob.Status.RUNNING);
        repo.update(running);
        ReviewJob retrying = insert("retrying");
        retrying.setStatus(ReviewJob.Status.RETRYING);
        repo.update(retrying);
        CountDownLatch recovered = new CountDownLatch(2);
        scheduler = scheduler(job -> { recovered.countDown(); return success(); },
            Duration.ofMillis(10), ignored -> true);

        scheduler.start();

        assertTrue(recovered.await(2, TimeUnit.SECONDS));
        awaitStatus(running, ReviewJob.Status.DONE);
        awaitStatus(retrying, ReviewJob.Status.DONE);
    }

    @Test
    void feedbackFailureDoesNotRerunReview() throws Exception {
        AtomicInteger executions = new AtomicInteger();
        scheduler = scheduler(job -> { executions.incrementAndGet(); return success(); },
            Duration.ofMillis(10), ignored -> { throw new RuntimeException("503"); });
        scheduler.start();
        ReviewJob job = insert("feedback");

        assertTrue(scheduler.submit(job));
        awaitStatus(job, ReviewJob.Status.DONE);
        assertEquals(1, executions.get());
    }

    @Test
    void stoppedSchedulerIsNotReadyAndRejectsNewJobs() {
        scheduler = scheduler(job -> success(), Duration.ofMillis(10), ignored -> true);
        scheduler.start();
        assertTrue(scheduler.isReady());

        scheduler.close();

        assertFalse(scheduler.isReady());
        assertFalse(scheduler.submit(newJob("after-stop")));
    }

    @Test
    void exhaustedRetriesCleanWorkspaceOnce() throws Exception {
        AtomicInteger executions = new AtomicInteger();
        AtomicInteger cleanups = new AtomicInteger();
        var executor = new com.codeguard.ci.executor.ReviewExecutor() {
            @Override public ReviewExecutionOutcome execute(ReviewJob job) {
                executions.incrementAndGet();
                return new ReviewExecutionOutcome.Failed(
                    FailureCode.PROCESS_TIMEOUT, "timeout", true, Duration.ZERO);
            }
            @Override public void cleanup(ReviewJob job) { cleanups.incrementAndGet(); }
        };
        scheduler = scheduler(executor, Duration.ofMillis(10), ignored -> true);
        scheduler.start();
        ReviewJob job = insert("exhausted");

        assertTrue(scheduler.submit(job));
        awaitStatus(job, ReviewJob.Status.FAILED);

        assertEquals(3, executions.get());
        assertEquals(2, job.getRetryCount());
        assertEquals(1, cleanups.get());
    }

    @Test
    void shutdownLeavesInterruptedJobRecoverable() throws Exception {
        CountDownLatch entered = new CountDownLatch(1);
        scheduler = scheduler(job -> {
            entered.countDown();
            try {
                new CountDownLatch(1).await();
                return success();
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return new ReviewExecutionOutcome.Failed(
                    FailureCode.INTERRUPTED, "interrupted", true, Duration.ZERO);
            }
        }, Duration.ofMillis(10), ignored -> true);
        scheduler.start();
        ReviewJob job = insert("shutdown");
        assertTrue(scheduler.submit(job));
        assertTrue(entered.await(1, TimeUnit.SECONDS));

        scheduler.close();
        try (JobRepository reopened = new JobRepository(dbPath)) {
            assertEquals(ReviewJob.Status.PENDING,
                reopened.findByDedupKey("a/b", 1, "shutdown").orElseThrow().getStatus());
        }
    }

    private JobScheduler scheduler(com.codeguard.ci.executor.ReviewExecutor executor,
                                   Duration retryDelay, FeedbackPublisher feedback) {
        return new JobScheduler(repo, 1, executor, retryDelay, Duration.ofSeconds(1), feedback);
    }

    private ReviewJob insert(String sha) {
        return repo.insert(newJob(sha)).orElseThrow();
    }

    private static ReviewJob newJob(String sha) {
        return new ReviewJob(new WebhookPayload("a/b", "url", 1, sha, "main", "head", 1L));
    }

    private static ReviewExecutionOutcome.Succeeded success() {
        return new ReviewExecutionOutcome.Succeeded(
            "{\"issues\":[],\"summary\":\"ok\"}", "diff", 0, Duration.ofMillis(1));
    }

    private void awaitStatus(ReviewJob job, ReviewJob.Status expected) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
        while (System.nanoTime() < deadline) {
            ReviewJob.Status actual = repo.findByDedupKey(job.getRepo(), job.getPrNumber(), job.getHeadSha())
                .orElseThrow().getStatus();
            if (actual == expected) return;
            Thread.onSpinWait();
        }
        fail("status did not become " + expected);
    }
}

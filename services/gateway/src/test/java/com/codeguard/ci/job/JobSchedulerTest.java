package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import com.codeguard.ci.model.ReviewJob.Status;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

import java.util.concurrent.atomic.AtomicInteger;

class JobSchedulerTest {

    private JobRepository repo;
    private JobScheduler scheduler;
    private AtomicInteger executeCount;

    @BeforeEach
    void setUp() {
        String dbPath = System.getProperty("java.io.tmpdir") + "/codeguard-sched-" + System.nanoTime();
        repo = new JobRepository(dbPath);
        executeCount = new AtomicInteger(0);
        scheduler = new JobScheduler(repo, 2, job -> {
            executeCount.incrementAndGet();
            job.setStatus(Status.DONE);
            repo.update(job);
        });
    }

    @AfterEach
    void tearDown() {
        repo.close();
    }

    @Test
    void shouldExecuteSubmittedJob() throws Exception {
        scheduler.start();
        var job = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "url", 1, "sha1", "main", "head", 1L)
        )).orElseThrow();

        scheduler.submit(job);
        Thread.sleep(500);

        var found = repo.findByDedupKey("a/b", 1, "sha1");
        assertEquals(Status.DONE, found.get().getStatus());
        assertEquals(1, executeCount.get());
    }

    @Test
    void shouldRecoverUnfinishedJobsOnStart() throws Exception {
        var j1 = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "u1", 1, "sha1", "main", "head", 1L))).orElseThrow();
        var j2 = repo.insert(new ReviewJob(
            new WebhookPayload("a/b", "u2", 2, "sha2", "main", "head", 1L))).orElseThrow();
        j2.setStatus(Status.RUNNING); // simulate crash
        repo.update(j2);

        scheduler.start();
        Thread.sleep(500);

        assertEquals(Status.DONE, repo.findByDedupKey("a/b", 1, "sha1").get().getStatus());
        assertEquals(Status.DONE, repo.findByDedupKey("a/b", 2, "sha2").get().getStatus());
        assertEquals(2, executeCount.get());
    }

    @Test
    void shouldRejectWhenQueueFull() {
        // Create scheduler with queue capacity effectively 0 by using maxConcurrency=1 and submitting many
        var tinyScheduler = new JobScheduler(repo, 1, job -> {
            try { Thread.sleep(5000); } catch (InterruptedException ignored) {}
            job.setStatus(Status.DONE);
            repo.update(job);
        });
        tinyScheduler.start();

        // Submit jobs rapidly — eventually the queue fills
        boolean allAccepted = true;
        for (int i = 0; i < 20; i++) {
            var job = new ReviewJob(new WebhookPayload("x/y", "u", i, "sha" + i, "main", "head", 1L));
            repo.insert(job);
            if (!tinyScheduler.submit(job)) {
                allAccepted = false;
                break;
            }
        }
        // At least one should be rejected (queue size 10 + 1 running = 11 slots, 20 submissions)
        assertFalse(allAccepted, "队列满时应拒绝提交");
    }
}

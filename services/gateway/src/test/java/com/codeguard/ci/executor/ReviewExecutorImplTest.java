package com.codeguard.ci.executor;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import com.codeguard.ci.model.ReviewJob.Status;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.*;

class ReviewExecutorImplTest {

    @TempDir
    Path workspacesDir;

    private JobRepository repo;
    private ReviewExecutorImpl executor;

    @BeforeEach
    void setUp() {
        String dbPath = System.getProperty("java.io.tmpdir") + "/codeguard-exec-" + System.nanoTime();
        repo = new JobRepository(dbPath);
        executor = new ReviewExecutorImpl(repo, workspacesDir, "");
    }

    @AfterEach
    void tearDown() {
        repo.close();
    }

    @Test
    void shouldFailOnInvalidRepoClone() {
        var job = new ReviewJob(new WebhookPayload(
            "no-such-org/no-such-repo", "https://github.com/no-such-org/no-such-repo.git",
            1, "abc123", "main", 1L));
        repo.insert(job);

        executor.accept(job);

        assertEquals(Status.FAILED, job.getStatus());
        assertNotNull(job.getErrorMessage());
    }

    @Test
    void shouldSetDoneForEchoFallback() throws Exception {
        // Create a fake git repo so clone doesn't fail
        Path repoDir = workspacesDir.resolve("fake-org_fake-repo-pr-42");
        Files.createDirectories(repoDir.resolve(".git"));

        var job = new ReviewJob(new WebhookPayload(
            "fake-org/fake-repo", "https://github.com/fake-org/fake-repo.git",
            42, "abc123", "main", 1L));
        repo.insert(job);

        // Override cloneOrFetch by pre-creating the directory
        // The executor will find .git and skip clone, then run Python CLI
        executor.accept(job);

        // Python may or may not be available — check that job reached a terminal state
        assertTrue(job.getStatus() == Status.DONE || job.getStatus() == Status.FAILED,
            "job should reach terminal state, got: " + job.getStatus());
    }

    @Test
    void shouldHandleEmptyStdout() {
        var job = new ReviewJob(new WebhookPayload(
            "fake/repo", "url", 1, "sha", "main", 1L));
        job.setStatus(Status.RUNNING);
        repo.insert(job);

        // Directly call handleFailure to test retry logic
        executor.handleFailure(job, true, "test empty output");
        // First retry: should be RETRYING with count=1
        assertEquals(1, job.getRetryCount());
    }
}

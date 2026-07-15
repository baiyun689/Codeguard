package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;

class JobRepositoryTest {

    private Path tmpDir;
    private JobRepository repo;

    @BeforeEach
    void setUp() throws IOException {
        tmpDir = Files.createTempDirectory("codeguard-h2-test-");
        repo = new JobRepository(tmpDir.resolve("testdb").toString());
    }

    @AfterEach
    void tearDown() {
        if (repo != null) {
            repo.close();
        }
        if (tmpDir != null) {
            try {
                Files.walk(tmpDir)
                    .sorted(Comparator.reverseOrder())
                    .forEach(p -> { try { Files.delete(p); } catch (IOException ignored) { } });
            } catch (IOException ignored) {
            }
        }
    }

    @Test
    void shouldInsertAndFindByDedupKey() {
        ReviewJob job = newJob("octocat/hello-world", 42, "abc123");
        Optional<ReviewJob> result = repo.insert(job);

        assertTrue(result.isPresent(), "首次插入应返回 job");
        assertEquals(Status.PENDING, result.get().getStatus());
        assertNotNull(result.get().getId(), "应自动生成 id");

        Optional<ReviewJob> found = repo.findByDedupKey("octocat/hello-world", 42, "abc123");
        assertTrue(found.isPresent(), "插入后应能查到");
        assertEquals(Status.PENDING, found.get().getStatus());
        assertEquals("abc123", found.get().getHeadSha());
    }

    @Test
    void shouldBeIdempotentOnDuplicateInsert() {
        ReviewJob first = newJob("octocat/hello-world", 42, "abc123");
        Optional<ReviewJob> result1 = repo.insert(first);
        assertTrue(result1.isPresent());

        // 相同去重键的第二次插入应返回空
        ReviewJob second = newJob("octocat/hello-world", 42, "abc123");
        Optional<ReviewJob> result2 = repo.insert(second);
        assertTrue(result2.isEmpty(), "重复插入应返回 Optional.empty()");
    }

    @Test
    void shouldUpdateStatus() {
        ReviewJob job = newJob("octocat/hello-world", 42, "abc123");
        repo.insert(job);

        // 修改状态并更新
        job.setStatus(Status.RUNNING);
        repo.update(job);

        Optional<ReviewJob> found = repo.findByDedupKey("octocat/hello-world", 42, "abc123");
        assertTrue(found.isPresent());
        assertEquals(Status.RUNNING, found.get().getStatus(), "update 后状态应为 RUNNING");
    }

    @Test
    void shouldFindUnfinishedJobs() {
        // 插入两个 job
        ReviewJob job1 = newJob("octocat/hello-world", 42, "abc123");
        ReviewJob job2 = newJob("octocat/hello-world", 99, "def456");
        repo.insert(job1);
        repo.insert(job2);

        // 将 job1 标记为 DONE 并更新
        job1.setStatus(Status.DONE);
        repo.update(job1);

        List<ReviewJob> unfinished = repo.findUnfinished();
        assertEquals(1, unfinished.size(), "应只剩 1 个未完成 job");
        assertEquals("def456", unfinished.get(0).getHeadSha());
    }

    @Test
    void pingReflectsIdempotentClose() {
        assertTrue(repo.ping());
        repo.close();
        assertFalse(repo.ping());
        assertDoesNotThrow(repo::close);
    }

    /** 创建测试用的 ReviewJob（最小字段） */
    private static ReviewJob newJob(String repo, int prNumber, String headSha) {
        ReviewJob job = new ReviewJob(repo, prNumber, headSha, "refs/heads/main",
            "https://github.com/" + repo + ".git");
        job.setInstallationId(12345L);
        return job;
    }
}

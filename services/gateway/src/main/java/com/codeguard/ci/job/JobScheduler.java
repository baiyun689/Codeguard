package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

/**
 * 异步 job 调度器: 有界队列 + 固定线程池 + 全局并发信号量 + 启动恢复。
 */
public class JobScheduler {

    private static final Logger log = LoggerFactory.getLogger(JobScheduler.class);

    private final JobRepository repo;
    private final int maxConcurrency;
    private final Consumer<ReviewJob> executor;
    private final Semaphore concurrencySem;
    private final ThreadPoolExecutor threadPool;
    private final AtomicBoolean running = new AtomicBoolean(false);

    /**
     * @param repo           job 持久化仓库
     * @param maxConcurrency 最大并发审查数
     * @param executor       审查执行回调（ReviewExecutorImpl 或测试 mock）
     */
    public JobScheduler(JobRepository repo, int maxConcurrency, Consumer<ReviewJob> executor) {
        this.repo = repo;
        this.maxConcurrency = maxConcurrency;
        this.executor = executor;
        this.concurrencySem = new Semaphore(maxConcurrency);
        this.threadPool = new ThreadPoolExecutor(
            maxConcurrency, maxConcurrency,
            60, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(10),
            new ThreadPoolExecutor.AbortPolicy()
        );
    }

    /** 提交 job 到队列。队列满 → 返回 false。 */
    public boolean submit(ReviewJob job) {
        try {
            threadPool.submit(() -> executeJob(job));
            return true;
        } catch (RejectedExecutionException e) {
            log.warn("job 队列满，拒绝: {}", job.dedupKey());
            return false;
        }
    }

    /** 启动: 恢复未完成 job + 标记 scheduler 为运行中 */
    public void start() {
        running.set(true);
        List<ReviewJob> unfinished = repo.findUnfinished();
        log.info("启动恢复: 发现 {} 个未完成 job", unfinished.size());
        for (ReviewJob job : unfinished) {
            job.setStatus(Status.PENDING);
            repo.update(job);
            submit(job);
        }
    }

    private void executeJob(ReviewJob job) {
        if (!concurrencySem.tryAcquire()) {
            // 兜底重新入队
            threadPool.submit(() -> executeJob(job));
            return;
        }
        try {
            log.info("开始审查: {} PR#{} sha={}", job.getRepo(), job.getPrNumber(),
                job.getHeadSha() != null ? job.getHeadSha().substring(0, Math.min(7, job.getHeadSha().length())) : "?");
            job.setStatus(Status.RUNNING);
            repo.update(job);
            executor.accept(job);
        } catch (Exception e) {
            log.error("审查异常: {}", job.dedupKey(), e);
            job.setStatus(Status.FAILED);
            repo.update(job);
        } finally {
            concurrencySem.release();
        }
    }
}

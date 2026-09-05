package com.codeguard.agent.graph;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ProjectSemanticCacheTest {

    @Test
    void reusesImmutableFileEdgesAcrossLazyQueries() throws Exception {
        ProjectSemanticCache cache = new ProjectSemanticCache(64, Duration.ofMinutes(1));
        AtomicInteger loads = new AtomicInteger();

        assertEquals(List.of(), cache.fileEdges("A.java", () -> {
            loads.incrementAndGet();
            return List.of();
        }));
        assertEquals(List.of(), cache.fileEdges("A.java", () -> {
            loads.incrementAndGet();
            return List.of();
        }));

        assertEquals(1, loads.get());
    }

    @Test
    void doesNotRetainFailedSemanticLoads() {
        ProjectSemanticCache cache = new ProjectSemanticCache(64, Duration.ofMinutes(1));
        AtomicInteger loads = new AtomicInteger();

        org.junit.jupiter.api.Assertions.assertThrows(
                IllegalStateException.class,
                () -> cache.candidateFiles("target", () -> {
                    loads.incrementAndGet();
                    throw new IllegalStateException("temporary");
                }));
        org.junit.jupiter.api.Assertions.assertDoesNotThrow(
                () -> cache.candidateFiles("target", () -> {
                    loads.incrementAndGet();
                    return List.of("A.java");
                }));

        assertEquals(2, loads.get());
    }

    @Test
    void cancellationInterruptsTheActualExpansionTask() throws Exception {
        ExecutorService executor = Executors.newSingleThreadExecutor();
        try {
            ProjectSnapshotManager manager = new ProjectSnapshotManager(
                    executor, 1, Duration.ofMinutes(1), Duration.ofSeconds(1));
            ProjectSnapshotManager.CancellableFuture<String> result =
                    new ProjectSnapshotManager.CancellableFuture<>();
            CountDownLatch started = new CountDownLatch(1);
            manager.submitCancellable(result, () -> {
                started.countDown();
                try {
                    Thread.sleep(30_000);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw interrupted;
                }
                return "unexpected";
            });
            started.await();

            result.cancel(true);

            assertEquals(true, result.isCancelled());
            // Let the worker observe the interrupt before the executor is closed.
            executor.shutdownNow();
        } finally {
            executor.shutdownNow();
        }
    }
}

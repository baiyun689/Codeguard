package com.codeguard.toolserver;

import com.codeguard.toolserver.ToolSessionManager.Session;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.nio.file.Files;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * 工具会话管理器测试:创建/取用/销毁,以及会话内工具注册。
 */
class ToolSessionManagerTest {

    private ToolSessionManager managerFor(Path allowedRoot) {
        return new ToolSessionManager(
                new com.codeguard.agent.graph.ProjectSnapshotManager(),
                new WorkspaceAccessPolicy(java.util.List.of(allowedRoot)));
    }

    private static void gitWorktree(Path repository) throws Exception {
        Files.createDirectories(repository.resolve(".git"));
    }

    @Test
    void createAndGetSession(@TempDir Path repo) throws Exception {
        gitWorktree(repo);
        ToolSessionManager mgr = managerFor(repo);
        String id = mgr.create(repo);

        assertNotNull(id);
        Session s = mgr.get(id);
        assertNotNull(s);
        assertEquals(id, s.getId());
        // 本期唯一工具应已注册到会话。
        assertNotNull(s.getTool("read_symbol"));
        assertNotNull(s.getTool("resolve_change_context"));
        assertNull(s.getTool("inspect_path"));
        assertNull(s.getTool("inspect_change_impact"));
        assertNull(s.getTool("inspect_structure"));
        assertNotNull(s.getSnapshot());
        // 未注册的工具返回 null,由控制器转成结构化错误。
        assertNull(s.getTool("get_call_graph"));
    }

    @Test
    void missingSessionReturnsNull() {
        ToolSessionManager mgr = managerFor(Path.of(System.getProperty("java.io.tmpdir")));
        assertNull(mgr.get(null));
        assertNull(mgr.get("nonexistent"));
    }

    @Test
    void removeSession(@TempDir Path repo) throws Exception {
        gitWorktree(repo);
        ToolSessionManager mgr = managerFor(repo);
        String id = mgr.create(repo);
        assertNotNull(mgr.get(id));

        mgr.remove(id);
        assertNull(mgr.get(id));
    }

    @Test
    void contextCarriesRepositoryRoot(@TempDir Path repo) throws Exception {
        gitWorktree(repo);
        ToolSessionManager mgr = managerFor(repo);
        String id = mgr.create(repo);
        Session s = mgr.get(id);

        assertEquals(repo.toAbsolutePath().normalize(), s.getContext().getRepoRoot());
        assertSame(s.getContext(), mgr.get(id).getContext());
    }

    @Test
    void rejectsRepositoryOutsideAllowedWorkspace(@TempDir Path root, @TempDir Path outside)
            throws Exception {
        gitWorktree(outside);
        ToolSessionManager manager = managerFor(root);

        assertThrows(WorkspaceAccessPolicy.RejectedWorkspaceException.class,
                () -> manager.create(outside, "head"));
    }

    @Test
    void rejectsRepositoryLinkThatResolvesOutsideAllowedWorkspace(
            @TempDir Path root,
            @TempDir Path outside
    ) throws Exception {
        gitWorktree(outside);
        Path linkedRepository = root.resolve("linked-repository");
        createSymbolicLink(linkedRepository, outside);
        ToolSessionManager manager = managerFor(root);

        assertThrows(WorkspaceAccessPolicy.RejectedWorkspaceException.class,
                () -> manager.create(linkedRepository, "head"));
    }

    private static void createSymbolicLink(Path link, Path target) throws Exception {
        try {
            Files.createSymbolicLink(link, target);
        } catch (java.nio.file.FileSystemException | UnsupportedOperationException exception) {
            Assumptions.assumeTrue(false, "当前系统不允许测试创建符号链接");
        }
    }
}

package com.codeguard.toolserver;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.graph.ProjectKey;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotManager;
import com.codeguard.agent.tools.FileAccessSandbox;
import com.codeguard.agent.tools.GetFileContentTool;
import com.codeguard.agent.tools.InspectChangeImpactTool;
import com.codeguard.agent.tools.InspectSecurityPathTool;
import com.codeguard.agent.tools.InspectStructureTool;
import com.codeguard.agent.tools.ResolveChangeContextTool;
import com.codeguard.agent.tools.ToolRegistry;

import java.nio.file.Path;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CompletableFuture;

/**
 * 工具会话管理器。
 * <p>
 * 为每次审查创建一个会话,持有该次审查的 {@link AgentContext}、沙箱与 per-session 工具注册表。
 * 会话超过 TTL 自动过期回收。所有工具调用经 {@code X-Session-Id} 关联到会话,
 * 会话不存在/过期则被上层拒绝。
 * <p>
 * 项目级 AST 和语义图由 {@link ProjectSnapshotManager} 跨同版本会话共享；
 * Session 持有 future 的直接引用，缓存淘汰不会中断活动审查。
 */
public final class ToolSessionManager {

    /** 会话存活时长:10 分钟。 */
    private static final long SESSION_TTL_MS = 10 * 60 * 1000L;

    private final ConcurrentHashMap<String, Session> sessions = new ConcurrentHashMap<>();
    private final ProjectSnapshotManager snapshotManager;

    public ToolSessionManager() {
        this(new ProjectSnapshotManager());
    }

    ToolSessionManager(ProjectSnapshotManager snapshotManager) {
        this.snapshotManager = snapshotManager;
    }

    /** 单次审查会话:不可变的范围信息 + 工具实例 + 创建时间。 */
    public static final class Session {
        private final String id;
        private final AgentContext context;
        private final ToolRegistry registry;
        private final ProjectKey projectKey;
        private final CompletableFuture<ProjectSnapshot> snapshot;
        private final long createdAt;

        Session(
                String id,
                Path repoRoot,
                Set<String> allowedFiles,
                String revision,
                ProjectSnapshotManager snapshotManager
        ) {
            this.id = id;
            this.context = new AgentContext(repoRoot, allowedFiles);
            this.createdAt = System.currentTimeMillis();
            this.projectKey = ProjectKey.of(repoRoot, revision);
            this.snapshot = snapshotManager.getOrBuild(projectKey);

            FileAccessSandbox sandbox = new FileAccessSandbox(repoRoot);
            this.registry = new ToolRegistry();
            // 加工具 = 在这里 register 一个实现即可,无需改协议(扩展接缝 design.md D2)。
            this.registry.register(new GetFileContentTool(sandbox, snapshot));
            this.registry.register(new ResolveChangeContextTool(snapshot));
            this.registry.register(new InspectSecurityPathTool(snapshot));
            this.registry.register(new InspectChangeImpactTool(snapshot));
            this.registry.register(new InspectStructureTool(snapshot));
        }

        public String getId() {
            return id;
        }

        public AgentContext getContext() {
            return context;
        }

        public AgentTool getTool(String name) {
            return registry.get(name);
        }

        public CompletableFuture<ProjectSnapshot> getSnapshot() {
            return snapshot;
        }

        ProjectKey getProjectKey() {
            return projectKey;
        }

        boolean isExpired() {
            return System.currentTimeMillis() - createdAt > SESSION_TTL_MS;
        }
    }

    /** 创建会话,返回唯一 session id。 */
    public String create(Path repoRoot, Set<String> allowedFiles) {
        return create(repoRoot, allowedFiles, "working-tree");
    }

    public String create(Path repoRoot, Set<String> allowedFiles, String revision) {
        cleanupExpired();
        String id = UUID.randomUUID().toString();
        sessions.put(id, new Session(
                id, repoRoot, allowedFiles, revision, snapshotManager));
        return id;
    }

    /** 取会话;不存在或已过期返回 {@code null}(过期的顺手清掉)。 */
    public Session get(String id) {
        if (id == null) {
            return null;
        }
        Session session = sessions.get(id);
        if (session == null) {
            return null;
        }
        if (session.isExpired()) {
            sessions.remove(id);
            return null;
        }
        return session;
    }

    public void remove(String id) {
        if (id != null) {
            Session removed = sessions.remove(id);
            if (removed != null) {
                snapshotManager.release(removed.getProjectKey());
            }
        }
    }

    private void cleanupExpired() {
        sessions.entrySet().removeIf(e -> e.getValue().isExpired());
    }

    public int activeSessionCount() {
        return sessions.size();
    }
}

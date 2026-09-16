package com.codeguard.toolserver;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.graph.ProjectKey;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotManager;
import com.codeguard.agent.graph.ProjectSnapshotProvider;
import com.codeguard.agent.graph.SourceSnapshotProvider;
import com.codeguard.agent.tools.QueryRelationsTool;
import com.codeguard.agent.tools.ReadSymbolTool;
import com.codeguard.agent.tools.ResolveChangeContextTool;
import com.codeguard.agent.tools.ToolRegistry;

import java.nio.file.Path;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CompletableFuture;

/**
 * 管理工具会话及其资源生命周期。
 *
 * 每次审查拥有独立 AgentContext 和工具注册表，通过 X-Session-Id 关联请求，
 * 超过 TTL 的会话自动回收。同版本会话共享项目轻量索引，语义关系按查询懒解析。
 */
public final class ToolSessionManager {

    /** 会话存活时长:10 分钟。 */
    private static final long SESSION_TTL_MS = 10 * 60 * 1000L;

    private final ConcurrentHashMap<String, Session> sessions = new ConcurrentHashMap<>();
    private final ProjectSnapshotManager snapshotManager;
    private final WorkspaceAccessPolicy workspaceAccessPolicy;

    ToolSessionManager(
            ProjectSnapshotManager snapshotManager,
            WorkspaceAccessPolicy workspaceAccessPolicy
    ) {
        this.snapshotManager = snapshotManager;
        this.workspaceAccessPolicy = workspaceAccessPolicy;
    }

    /** 单次审查会话:不可变的范围信息 + 工具实例 + 创建时间。 */
    public static final class Session {
        private final String id;
        private final AgentContext context;
        private final ToolRegistry registry;
        private final ProjectKey projectKey;
        private final ProjectSnapshotManager snapshotManager;
        private volatile CompletableFuture<ProjectSnapshot> snapshot;
        private final ProjectSnapshotProvider snapshotProvider;
        private final long createdAt;

        Session(
                String id,
                Path repoRoot,
                String revision,
                ProjectSnapshotManager snapshotManager
        ) {
            this.id = id;
            this.context = new AgentContext(repoRoot);
            this.createdAt = System.currentTimeMillis();
            this.projectKey = ProjectKey.of(repoRoot, revision);
            this.snapshotManager = snapshotManager;
            // 会话创建时不启动项目索引构建；源码可独立读取，完整快照在实际访问时加载。
            this.snapshot = null;
            this.snapshotProvider = snapshotManager.lazyProvider(projectKey);

            this.registry = new ToolRegistry();
            // 加工具 = 在这里 register 一个实现即可,无需改协议(扩展接缝 design.md D2)。
            // 注册受控审查使用的符号解析、源码读取和关系查询工具。
            this.registry.register(new ReadSymbolTool(snapshotProvider));
            this.registry.register(new QueryRelationsTool(snapshotProvider));
            this.registry.register(new ResolveChangeContextTool(snapshotProvider));
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
            CompletableFuture<ProjectSnapshot> current = snapshot;
            if (current != null) {
                return current;
            }
            synchronized (this) {
                if (snapshot == null) {
                    snapshot = snapshotManager.getOrBuildIndex(projectKey);
                }
                return snapshot;
            }
        }

        ProjectKey getProjectKey() {
            return projectKey;
        }

        boolean isExpired() {
            return System.currentTimeMillis() - createdAt > SESSION_TTL_MS;
        }
    }

    /** 创建会话,返回唯一 session id。 */
    public String create(Path repoRoot) {
        return create(repoRoot, "working-tree");
    }

    public String create(Path repoRoot, String revision) {
        cleanupExpired();
        Path approvedRoot = workspaceAccessPolicy.requireReviewRepository(repoRoot);
        String id = UUID.randomUUID().toString();
        sessions.put(id, new Session(
                id, approvedRoot, revision, snapshotManager));
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

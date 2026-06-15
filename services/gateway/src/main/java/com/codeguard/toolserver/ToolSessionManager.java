package com.codeguard.toolserver;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.repomap.RepoMapBuilder;
import com.codeguard.agent.tools.FileAccessSandbox;
import com.codeguard.agent.tools.GetFileContentTool;
import com.codeguard.agent.tools.GetRepoMapTool;
import com.codeguard.agent.tools.ToolRegistry;

import java.nio.file.Path;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 工具会话管理器。
 * <p>
 * 为每次审查创建一个会话,持有该次审查的 {@link AgentContext}、沙箱与 per-session 工具注册表。
 * 会话超过 TTL 自动过期回收。所有工具调用经 {@code X-Session-Id} 关联到会话,
 * 会话不存在/过期则被上层拒绝。
 * <p>
 * 扩展接缝(design.md D3):后续 AST / 调用图 / 语义检索 / 记忆等**重资源**需要"一次审查内、
 * 甚至跨会话按仓库共享"。预留的挂载点见下方 {@code // TODO(阶段3后段)} 注释——
 * 本期 get_file_content 是无状态只读,不需要共享缓存,故只立结构、不填充。
 */
public final class ToolSessionManager {

    /** 会话存活时长:10 分钟。 */
    private static final long SESSION_TTL_MS = 10 * 60 * 1000L;

    private final ConcurrentHashMap<String, Session> sessions = new ConcurrentHashMap<>();

    // TODO(阶段3后段):按 repoRoot 共享的重资源缓存挂这里,例如
    //   private final ConcurrentHashMap<Path, SharedProjectResources> projectResources = ...;
    // SharedProjectResources 持有 CallGraph / 向量索引等,供同仓库的多个会话复用。
    // 本期不实现共享,仅预留位置,避免后续加重型工具时回头改会话层。

    /** 单次审查会话:不可变的范围信息 + 工具实例 + 创建时间。 */
    public static final class Session {
        private final String id;
        private final AgentContext context;
        private final ToolRegistry registry;
        private final long createdAt;

        Session(String id, Path repoRoot, Set<String> allowedFiles) {
            this.id = id;
            this.context = new AgentContext(repoRoot, allowedFiles);
            this.createdAt = System.currentTimeMillis();

            FileAccessSandbox sandbox = new FileAccessSandbox(repoRoot, allowedFiles);
            this.registry = new ToolRegistry();
            // 加工具 = 在这里 register 一个实现即可,无需改协议(扩展接缝 design.md D2)。
            this.registry.register(new GetFileContentTool(sandbox));
            // get_repo_map:无状态,从 context 拿 repoRoot + diff 种子现算地图。
            this.registry.register(new GetRepoMapTool(new RepoMapBuilder()));
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

        boolean isExpired() {
            return System.currentTimeMillis() - createdAt > SESSION_TTL_MS;
        }
    }

    /** 创建会话,返回唯一 session id。 */
    public String create(Path repoRoot, Set<String> allowedFiles) {
        cleanupExpired();
        String id = UUID.randomUUID().toString();
        sessions.put(id, new Session(id, repoRoot, allowedFiles));
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
            sessions.remove(id);
        }
    }

    private void cleanupExpired() {
        sessions.entrySet().removeIf(e -> e.getValue().isExpired());
    }

    public int activeSessionCount() {
        return sessions.size();
    }
}

package com.codeguard.toolserver;

import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.toolserver.ToolSessionManager.Session;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.javalin.Javalin;
import io.javalin.http.Context;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.Set;

/**
 * 工具服务的 HTTP 端点控制器。
 * <p>
 * 路由设计(design.md D2):
 * <ul>
 *   <li>{@code POST /api/v1/tools/session} 创建会话(repo 路径 + 改动文件集合)→ session_id;</li>
 *   <li>{@code DELETE /api/v1/tools/session/{id}} 销毁会话;</li>
 *   <li>{@code POST /api/v1/tools/{name}} **通用分发**:凭 X-Session-Id 关联会话,按 name 查注册表执行。</li>
 * </ul>
 * 统一响应信封:成功 {@code {success:true, result:...}},失败 {@code {success:false, error:...}}。
 * 注意:{@code session} 是保留路径段,不会被当成工具名分发。
 */
public final class ToolServerController {

    private static final Logger log = LoggerFactory.getLogger(ToolServerController.class);
    private static final String SESSION_HEADER = "X-Session-Id";

    private final ObjectMapper mapper = new ObjectMapper();
    private final ToolSessionManager sessionManager = new ToolSessionManager();

    public void registerRoutes(Javalin app) {
        app.post("/api/v1/tools/session", this::handleCreateSession);
        app.delete("/api/v1/tools/session/{sessionId}", this::handleDeleteSession);
        // 通用分发:{name} 形参承接所有工具名。session 已由更具体的上面两条路由抢先匹配。
        app.post("/api/v1/tools/{name}", this::handleToolCall);
        log.info("工具服务端点已注册");
    }

    private void handleCreateSession(Context ctx) {
        try {
            JsonNode body = mapper.readTree(ctx.body());
            String repoDir = textOrEmpty(body, "repo_path");
            if (repoDir.isEmpty()) {
                ctx.json(error("缺少 repo_path"));
                return;
            }
            Set<String> allowedFiles = new LinkedHashSet<>();
            JsonNode arr = body.path("allowed_files");
            if (arr.isArray()) {
                arr.forEach(n -> allowedFiles.add(n.asText()));
            }

            String sessionId = sessionManager.create(Path.of(repoDir), allowedFiles);
            log.info("创建工具会话: {}(允许文件 {} 个)", sessionId, allowedFiles.size());

            ObjectNode resp = success(null);
            resp.put("session_id", sessionId);
            ctx.json(resp);
        } catch (Exception e) {
            log.error("创建会话失败", e);
            ctx.json(error("创建会话失败: " + e.getMessage()));
        }
    }

    private void handleDeleteSession(Context ctx) {
        sessionManager.remove(ctx.pathParam("sessionId"));
        ctx.json(success(null));
    }

    private void handleToolCall(Context ctx) {
        String toolName = ctx.pathParam("name");
        String sessionId = ctx.header(SESSION_HEADER);

        Session session = sessionManager.get(sessionId);
        if (session == null) {
            // 缺失/过期一律拒绝,绝不执行任何文件访问。
            ctx.json(error("会话不存在或已过期: " + (sessionId == null ? "(缺少 " + SESSION_HEADER + ")" : sessionId)));
            return;
        }

        AgentTool tool = session.getTool(toolName);
        if (tool == null) {
            ctx.json(error("未知工具: " + toolName));
            return;
        }

        try {
            JsonNode body = mapper.readTree(ctx.body());
            // 本期工具均为单字符串输入:文件类取 file_path,查询类取 query;都没有则空串。
            String input = firstNonEmpty(textOrEmpty(body, "file_path"), textOrEmpty(body, "query"));

            int n = session.getContext().incrementToolCalls();
            ToolResult result = tool.execute(input, session.getContext());
            // 记录工具调用,便于观测"工具利用率"(对照实验指标)与排障。
            log.info("工具调用 [{}] {}(\"{}\") -> {}", session.getId(), toolName, input,
                    result.isSuccess() ? "ok" : "err:" + result.getError());
            ctx.json(result.isSuccess() ? success(result.getResult()) : error(result.getError()));
        } catch (Exception e) {
            log.error("工具执行失败: {}", toolName, e);
            ctx.json(error("工具执行失败: " + e.getMessage()));
        }
    }

    // --- helpers ---

    private static String textOrEmpty(JsonNode node, String field) {
        JsonNode v = node.path(field);
        return v.isMissingNode() || v.isNull() ? "" : v.asText();
    }

    private static String firstNonEmpty(String a, String b) {
        return !a.isEmpty() ? a : b;
    }

    private ObjectNode success(String result) {
        ObjectNode node = mapper.createObjectNode();
        node.put("success", true);
        if (result != null) {
            node.put("result", result);
        }
        return node;
    }

    private ObjectNode error(String message) {
        ObjectNode node = mapper.createObjectNode();
        node.put("success", false);
        if (message != null) {
            node.put("error", message);
        }
        return node;
    }
}

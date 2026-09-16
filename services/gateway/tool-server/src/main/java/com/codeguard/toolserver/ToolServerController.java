package com.codeguard.toolserver;

import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.common.GatewayMetrics;
import com.codeguard.toolserver.ToolSessionManager.Session;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.file.Path;

/**
 * 工具服务的 HTTP 端点控制器。
 * <p>
 * 路由设计(design.md D2):
 * <ul>
 *   <li>{@code POST /api/v1/tools/session} 创建项目快照会话(repo 路径 + revision)→ session_id;</li>
 *   <li>{@code DELETE /api/v1/tools/session/{id}} 销毁会话;</li>
 *   <li>{@code POST /api/v1/tools/{name}} **通用分发**:凭 X-Session-Id 关联会话,按 name 查注册表执行。</li>
 * </ul>
 * 统一响应信封:成功 {@code {success:true, result:...}},失败 {@code {success:false, error:...}}。
 * 注意:{@code session} 是保留路径段,不会被当成工具名分发。
 */
@RestController
public final class ToolServerController {

    private static final Logger log = LoggerFactory.getLogger(ToolServerController.class);
    private static final String SESSION_HEADER = "X-Session-Id";

    private final ObjectMapper mapper;
    private final ToolSessionManager sessionManager;
    private final GatewayMetrics metrics;

    public ToolServerController(GatewayMetrics metrics, ToolSessionManager sessionManager, ObjectMapper mapper) {
        this.metrics = metrics;
        this.sessionManager = sessionManager;
        this.mapper = mapper;
        metrics.gaugeToolSessions(sessionManager, ToolSessionManager::activeSessionCount);
    }

    @PostMapping("/api/v1/tools/session")
    public ResponseEntity<?> handleCreateSession(@RequestBody(required = false) String rawBody) {
        try {
            JsonNode body = mapper.readTree(rawBody == null ? "" : rawBody);
            String repoDir = textOrEmpty(body, "repo_path");
            if (repoDir.isEmpty()) {
                return ResponseEntity.ok(error("缺少 repo_path"));
            }
            String revision = textOrEmpty(body, "revision");
            String sessionId = sessionManager.create(Path.of(repoDir), revision);
            log.info("创建工具会话: {}", sessionId);

            ObjectNode resp = success(null);
            resp.put("session_id", sessionId);
            return ResponseEntity.ok(resp);
        } catch (WorkspaceAccessPolicy.RejectedWorkspaceException e) {
            return ResponseEntity.status(400).body(error(e.getMessage()));
        } catch (Exception e) {
            log.error("创建会话失败", e);
            return ResponseEntity.ok(error("创建会话失败: " + e.getMessage()));
        }
    }

    @DeleteMapping("/api/v1/tools/session/{sessionId}")
    public ResponseEntity<?> handleDeleteSession(@PathVariable("sessionId") String sessionId) {
        sessionManager.remove(sessionId);
        return ResponseEntity.ok(success(null));
    }

    @PostMapping("/api/v1/tools/{name}")
    public ResponseEntity<?> handleToolCall(@PathVariable("name") String toolName,
            HttpServletRequest request, @RequestBody(required = false) String rawBody) {
        String sessionId = request.getHeader(SESSION_HEADER);

        Session session = sessionManager.get(sessionId);
        if (session == null) {
            // 缺失/过期一律拒绝,绝不执行任何文件访问。
            return ResponseEntity.ok(error("会话不存在或已过期: " + (sessionId == null ? "(缺少 " + SESSION_HEADER + ")" : sessionId)));
        }

        AgentTool tool = session.getTool(toolName);
        if (tool == null) {
            return ResponseEntity.ok(error("未知工具: " + toolName));
        }

        try {
            JsonNode body = mapper.readTree(rawBody == null ? "" : rawBody);
            // 工具请求统一承载在 query 字符串中。源码工具已经是 symbol-only
            // 契约，旧的 file_path 入参直接拒绝，避免协议表面上继续支持路径读取。
            if (toolName.equals("read_symbol") && body.has("file_path")) {
                return ResponseEntity.ok(error("symbol_id_only"));
            }
            String input = textOrEmpty(body, "query");

            int n = session.getContext().incrementToolCalls();
            ToolResult result = tool.execute(input, session.getContext());
            metrics.toolCall(toolName, result.isSuccess() ? "success" : "error");
            // 记录工具调用,便于观测"工具利用率"(对照实验指标)与排障。
            log.info("工具调用 [{}] {}(\"{}\") -> {}", session.getId(), toolName, input,
                    result.isSuccess() ? "ok" : "err:" + result.getError());
            return ResponseEntity.ok(result.isSuccess() ? success(result.getResult()) : error(result.getError()));
        } catch (Exception e) {
            metrics.toolCall(toolName, "error");
            log.error("工具执行失败: {}", toolName, e);
            return ResponseEntity.ok(error("工具执行失败: " + e.getMessage()));
        }
    }

    // --- helpers ---

    private static String textOrEmpty(JsonNode node, String field) {
        JsonNode v = node.path(field);
        return v.isMissingNode() || v.isNull() ? "" : v.asText();
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

package com.codeguard.toolserver;

import io.javalin.Javalin;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Codeguard 工具服务应用。
 * <p>
 * 用 Javalin 暴露工具回调端点 + 健康检查,可作为独立进程启动,供 Python Agent 回调。
 * 默认端口 9090,可用环境变量 {@code CODEGUARD_TOOL_SERVER_PORT} 覆盖。
 */
public final class ToolServerApp {

    private static final Logger log = LoggerFactory.getLogger(ToolServerApp.class);
    private static final int DEFAULT_PORT = 9090;

    private final Javalin app;

    public ToolServerApp() {
        this.app = Javalin.create(cfg -> {
            cfg.showJavalinBanner = false;
            cfg.http.maxRequestSize = 10_000_000L;
        });
        new ToolServerController().registerRoutes(app);
        app.get("/health", ctx -> ctx.result("OK"));

        // CI 集成: webhook 端点
        String webhookSecret = System.getenv("CODEGUARD_WEBHOOK_SECRET");
        if (webhookSecret != null && !webhookSecret.isBlank()) {
            var jobRepo = new com.codeguard.ci.job.JobRepository("./data/codeguard-jobs");
            var scheduler = new com.codeguard.ci.job.JobScheduler(jobRepo, 2, job -> {});
            scheduler.start();
            var webhookCtrl = new com.codeguard.ci.webhook.GitHubWebhookController(webhookSecret, jobRepo, scheduler);
            webhookCtrl.register(app);
            log.info("GitHub webhook 端点已启用: POST /webhooks/github");
        }
    }

    public static int resolvePort() {
        String env = System.getenv("CODEGUARD_TOOL_SERVER_PORT");
        if (env != null && !env.isBlank()) {
            return Integer.parseInt(env.trim());
        }
        return DEFAULT_PORT;
    }

    public void start(int port) {
        app.start(port);
        log.info("Codeguard 工具服务已启动,端口 {}", port);
        log.info("  POST   /api/v1/tools/session       创建会话");
        log.info("  DELETE /api/v1/tools/session/{{id}}  销毁会话");
        log.info("  POST   /api/v1/tools/{{name}}        工具调用(需 X-Session-Id)");
        log.info("  GET    /health                     健康检查");
    }

    public void stop() {
        app.stop();
        log.info("Codeguard 工具服务已停止");
    }

    /** 暴露底层 Javalin 实例,便于测试中按需取用。 */
    public Javalin javalin() {
        return app;
    }
}

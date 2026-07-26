package com.codeguard.toolserver;

import com.codeguard.common.GatewayMetrics;
import com.codeguard.common.OperationalController;
import io.javalin.Javalin;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** Agent 工具服务 —— 文件沙箱 + AST 分析 + 危险 API 扫描等。监听端口 9090。 */
public final class ToolServerApp {
    private static final Logger log = LoggerFactory.getLogger(ToolServerApp.class);

    private final Javalin app;
    private final GatewaySettings settings;
    private final GatewayMetrics metrics;

    public ToolServerApp() {
        this(GatewaySettings.fromEnv());
    }

    ToolServerApp(GatewaySettings settings) {
        this.settings = settings;
        this.metrics = new GatewayMetrics();
        this.app = Javalin.create(cfg -> {
            cfg.showJavalinBanner = false;
            cfg.http.maxRequestSize = 10_000_000L;
        });
        new ToolServerController(metrics, settings).registerRoutes(app);
        new OperationalController(this::ready, metrics).register(app);
    }

    private boolean ready() { return true; }

    public int port() { return settings.port(); }

    public void start(int port) {
        app.start(port);
        log.info("Tool Server 已启动, 端口 {}", port);
    }

    public void stop() {
        app.stop();
        log.info("Tool Server 已停止");
    }

    public Javalin javalin() { return app; }
}

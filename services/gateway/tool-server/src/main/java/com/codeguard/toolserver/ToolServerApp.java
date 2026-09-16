package com.codeguard.toolserver;

import com.codeguard.common.GatewayApplication;
import java.util.Map;

/** 工具服务的独立 Spring Boot 上下文，与公共 Webhook 端口隔离。 */
public final class ToolServerApp {
    private final GatewaySettings settings;
    private GatewayApplication application;
    public ToolServerApp() { this(GatewaySettings.fromEnv()); }
    ToolServerApp(GatewaySettings settings) { this.settings = settings; }
    public int port() { return application == null ? settings.port() : application.port(); }
    public void start(int port) {
        if (application != null) throw new IllegalStateException("Tool server already started");
        application = GatewayApplication.start(ToolServerConfiguration.class, port, Map.of("gatewaySettings", settings));
    }
    public void stop() { if (application != null) { application.close(); application = null; } }
}

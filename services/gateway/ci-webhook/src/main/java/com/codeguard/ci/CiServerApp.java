package com.codeguard.ci;

import com.codeguard.common.GatewayApplication;
import com.codeguard.toolserver.GatewaySettings;
import java.util.Map;

/** 公共 Webhook 服务入口，业务组件由 Spring 容器管理。 */
public final class CiServerApp {
    private final GatewaySettings settings;
    private GatewayApplication application;
    public CiServerApp() { this(GatewaySettings.fromEnv()); }
    CiServerApp(GatewaySettings settings) { this.settings = settings; }
    public void start() {
        if (application != null) throw new IllegalStateException("CI server already started");
        int port = Integer.parseInt(System.getenv().getOrDefault("CODEGUARD_CI_PORT", "8080"));
        application = GatewayApplication.start(CiServerConfiguration.class, port, Map.of("gatewaySettings", settings));
    }
    public void stop() { if (application != null) { application.close(); application = null; } }
}

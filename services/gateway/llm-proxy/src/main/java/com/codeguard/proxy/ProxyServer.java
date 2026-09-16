package com.codeguard.proxy;

import com.codeguard.common.GatewayApplication;
import com.codeguard.proxy.config.ProxyConfig;
import java.util.Map;

/** 模型代理服务入口，支持独立或组合启动，默认监听 9091 端口。 */
public final class ProxyServer {
    private final ProxyConfig config;
    private GatewayApplication application;
    private final int configuredPort;
    public ProxyServer() { this(ProxyConfig.load()); }
    ProxyServer(ProxyConfig config) {
        this.config = config;
        this.configuredPort = Integer.parseInt(System.getenv().getOrDefault("CODEGUARD_LLM_PROXY_PORT", "9091"));
    }
    public void start() {
        if (application != null) throw new IllegalStateException("Proxy already started");
        application = GatewayApplication.start(ProxyConfiguration.class, configuredPort, Map.of("proxyConfig", config));
    }
    public void stop() { if (application != null) { application.close(); application = null; } }
    public int port() { return application == null ? configuredPort : application.port(); }
    public static void main(String[] args) { new ProxyServer().start(); }
}

package com.codeguard;

import com.codeguard.ci.CiServerApp;
import com.codeguard.proxy.ProxyServer;
import com.codeguard.toolserver.ToolServerApp;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Codeguard Gateway 统一入口。
 * 在同一个 JVM 进程内启动三个独立服务:
 *   Tool Server  — 端口 9090（Agent 工具 + 文件沙箱 + AST 分析）
 *   CI Webhook   — 端口 8080（GitHub PR 自动审查链路）
 *   LLM Proxy    — 端口 9091（OpenAI 兼容 LLM 代理网关）
 */
public final class Main {
    private static final Logger log = LoggerFactory.getLogger(Main.class);

    private Main() {}

    public static void main(String[] args) {
        ToolServerApp toolServer = new ToolServerApp();
        toolServer.start(toolServer.port());

        CiServerApp ciServer = new CiServerApp();
        ciServer.start();

        ProxyServer proxyServer = new ProxyServer();
        proxyServer.start();

        log.info("================================================");
        log.info("Codeguard Gateway 全部就绪:");
        log.info("  Tool Server : http://localhost:{}", toolServer.port());
        log.info("  CI Webhook  : http://localhost:{}",
            System.getenv().getOrDefault("CODEGUARD_CI_PORT", "8080"));
        log.info("  LLM Proxy   : http://localhost:{}",
            System.getenv().getOrDefault("CODEGUARD_LLM_PROXY_PORT", "9091"));
        log.info("================================================");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            log.info("正在关闭所有服务...");
            proxyServer.stop();
            ciServer.stop();
            toolServer.stop();
        }));
    }
}

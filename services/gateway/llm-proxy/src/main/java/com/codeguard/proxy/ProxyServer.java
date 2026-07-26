package com.codeguard.proxy;

import com.codeguard.common.OperationalController;
import com.codeguard.proxy.adapter.ClaudeAdapter;
import com.codeguard.proxy.adapter.DeepSeekAdapter;
import com.codeguard.proxy.adapter.LlmAdapter;
import com.codeguard.proxy.adapter.QwenAdapter;
import com.codeguard.proxy.config.ProxyConfig;
import com.codeguard.proxy.handler.ChatCompletionsHandler;
import com.codeguard.proxy.resilience.ResilienceService;
import com.codeguard.proxy.router.ProviderRouter;
import io.javalin.Javalin;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * LLM Proxy 服务 —— OpenAI 兼容的 LLM 代理网关。
 *
 * 监听端口 9091（可通过 CODEGUARD_LLM_PROXY_PORT 环境变量覆盖）。
 * 提供 /v1/chat/completions 端点,按 model 名路由到不同提供商并做协议转换。
 * 韧性层：Resilience4j 限流 + 熔断 + 重试。
 */
public final class ProxyServer {
    private static final Logger log = LoggerFactory.getLogger(ProxyServer.class);

    private final Javalin app;
    private final int port;
    private final boolean ready;

    public ProxyServer() {
        this(ProxyConfig.load());
    }

    ProxyServer(ProxyConfig config) {
        this.port = Integer.parseInt(System.getenv().getOrDefault("CODEGUARD_LLM_PROXY_PORT", "9091"));

        // Build adapters
        ResilienceService resilience = new ResilienceService(config.resilience());
        Map<String, LlmAdapter> adapters = new LinkedHashMap<>();

        for (var entry : config.providers().entrySet()) {
            String name = entry.getKey();
            ProxyConfig.ProviderConfig pc = entry.getValue();
            if (pc.url().isBlank()) {
                log.warn("Provider '{}' 未配置 URL, 跳过", name);
                continue;
            }
            var cb = resilience.circuitBreakerFor(name);
            LlmAdapter adapter = switch (name) {
                case "deepseek" -> new DeepSeekAdapter(pc.url(), pc.key(), cb);
                case "claude" -> new ClaudeAdapter(pc.url(), pc.key(), cb);
                case "qwen" -> new QwenAdapter(pc.url(), pc.key(), cb);
                default -> {
                    // Unknown provider type → try as OpenAI-compatible (like DeepSeek)
                    log.info("未知 provider 类型 '{}', 尝试 OpenAI 兼容模式", name);
                    yield new DeepSeekAdapter(pc.url(), pc.key(), cb);
                }
            };
            adapters.put(name, adapter);
            log.info("Provider 已注册: {} → {}", name, pc.url());
        }

        if (adapters.isEmpty()) {
            log.warn("没有配置任何 LLM provider, LLM Proxy 将以空路由启动");
        }

        // Build router
        ProviderRouter router = new ProviderRouter(config, adapters);
        this.ready = !adapters.isEmpty() && !router.routes().isEmpty();

        // Build app
        this.app = Javalin.create(cfg -> {
            cfg.showJavalinBanner = false;
            cfg.http.maxRequestSize = 10_000_000L;
        });

        // Routes
        app.post("/v1/chat/completions", new ChatCompletionsHandler(router, resilience));
        new OperationalController(this::ready).register(app);

        // Exception handler for unexpected errors
        app.exception(Exception.class, (e, ctx) -> {
            log.error("LLM Proxy 未预期异常", e);
            ctx.status(500).json(com.codeguard.proxy.model.OpenAiChatResponse.error(
                "Internal proxy error: " + e.getMessage(), "proxy_error", "500"));
        });
    }

    private boolean ready() {
        return ready;
    }

    public void start() {
        app.start(port);
        log.info("LLM Proxy 已启动, 端口 {} (OpenAI 兼容端点: /v1/chat/completions)", port);
    }

    public void stop() {
        app.stop();
        log.info("LLM Proxy 已停止");
    }

    public Javalin javalin() { return app; }
    public int port() { return port; }

    public static void main(String[] args) {
        ProxyServer server = new ProxyServer();
        server.start();
        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));
    }
}

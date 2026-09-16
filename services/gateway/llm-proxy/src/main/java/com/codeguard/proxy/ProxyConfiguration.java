package com.codeguard.proxy;

import com.codeguard.common.*;
import com.codeguard.proxy.adapter.*;
import com.codeguard.proxy.config.ProxyConfig;
import com.codeguard.proxy.handler.ChatCompletionsHandler;
import com.codeguard.proxy.resilience.ResilienceService;
import com.codeguard.proxy.router.ProviderRouter;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import java.util.LinkedHashMap;
import java.util.Map;

@SpringBootConfiguration(proxyBeanMethods = false)
@EnableAutoConfiguration(exclude = DataSourceAutoConfiguration.class)
@Import(GatewayHttpConfiguration.class)
public class ProxyConfiguration {
    @Bean GatewayMetrics gatewayMetrics() { return new GatewayMetrics(); }
    @Bean(initMethod = "start", destroyMethod = "close")
    AlertEvaluator alertEvaluator(GatewayMetrics metrics) { return new AlertEvaluator(metrics); }
    @Bean ResilienceService resilienceService(ProxyConfig config, GatewayMetrics metrics) {
        return new ResilienceService(config.resilience(), metrics);
    }
    @Bean Map<String, LlmAdapter> llmAdapters(ProxyConfig config, ResilienceService resilience) {
        Map<String, LlmAdapter> adapters = new LinkedHashMap<>();
        config.providers().forEach((name, provider) -> {
            if (provider.url().isBlank()) return;
            var breaker = resilience.circuitBreakerFor(name);
            LlmAdapter adapter = switch (name) {
                case "deepseek" -> new DeepSeekAdapter(provider.url(), provider.key(), breaker);
                case "claude" -> new ClaudeAdapter(provider.url(), provider.key(), breaker);
                case "qwen" -> new QwenAdapter(provider.url(), provider.key(), breaker);
                default -> new DeepSeekAdapter(name, provider.url(), provider.key(), breaker);
            };
            adapters.put(name, adapter);
        });
        return adapters;
    }
    @Bean ProviderRouter providerRouter(ProxyConfig config,
            @Qualifier("llmAdapters") Map<String, LlmAdapter> adapters) {
        return new ProviderRouter(config, adapters);
    }
    @Bean ChatCompletionsHandler chatCompletionsHandler(ProviderRouter router, ResilienceService resilience) {
        return new ChatCompletionsHandler(router, resilience);
    }
    @Bean OperationalController operationalController(ProviderRouter router,
            @Qualifier("llmAdapters") Map<String, LlmAdapter> adapters, GatewayMetrics metrics, AlertEvaluator alerts) {
        return new OperationalController(() -> !adapters.isEmpty() && !router.routes().isEmpty(), metrics, alerts);
    }
    @Bean ProxyExceptionHandler proxyExceptionHandler() { return new ProxyExceptionHandler(); }
}

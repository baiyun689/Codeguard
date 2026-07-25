package com.codeguard.proxy.router;

import com.codeguard.proxy.adapter.LlmAdapter;
import com.codeguard.proxy.config.ProxyConfig;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * 按 model 名路由到 provider 降级链。
 * 路由表从 ProxyConfig 加载，未匹配的 model 返回空链。
 */
public final class ProviderRouter {
    private static final Logger log = LoggerFactory.getLogger(ProviderRouter.class);

    private final Map<String, List<LlmAdapter>> routes;
    private final List<LlmAdapter> defaultFallback;

    public ProviderRouter(ProxyConfig config, Map<String, LlmAdapter> adapters) {
        // Build route map from config
        var routeMap = new java.util.LinkedHashMap<String, List<LlmAdapter>>();
        for (var entry : config.routes().entrySet()) {
            String modelName = entry.getKey();
            List<String> chain = entry.getValue().chain();
            List<LlmAdapter> adapterChain = new ArrayList<>();
            for (String providerName : chain) {
                LlmAdapter adapter = adapters.get(providerName);
                if (adapter != null) {
                    adapterChain.add(adapter);
                } else {
                    log.warn("路由 {} 引用了未知 provider '{}', 已跳过", modelName, providerName);
                }
            }
            if (!adapterChain.isEmpty()) {
                routeMap.put(modelName, adapterChain);
            }
        }
        this.routes = Collections.unmodifiableMap(routeMap);

        // Default fallback: if no specific route, try all configured adapters
        this.defaultFallback = List.copyOf(adapters.values());

        log.info("路由表已加载: {} 条路由, {} 个 provider",
            routes.size(), adapters.size());
        for (var entry : routes.entrySet()) {
            List<String> names = entry.getValue().stream()
                .map(LlmAdapter::providerName).toList();
            log.info("  {} → [{}]", entry.getKey(), String.join(", ", names));
        }
    }

    /**
     * 解析 model 对应的降级链。
     * @return 有序的 adapter 列表（主 → fallback1 → fallback2），未匹配时返回默认全链
     */
    public List<LlmAdapter> resolveChain(String modelName) {
        if (modelName == null || modelName.isBlank()) {
            return defaultFallback;
        }
        List<LlmAdapter> chain = routes.get(modelName);
        if (chain != null && !chain.isEmpty()) {
            return chain;
        }
        // Fuzzy match: check if any registered model contains the requested name
        for (var entry : routes.entrySet()) {
            if (entry.getKey().contains(modelName) || modelName.contains(entry.getKey())) {
                log.info("模糊匹配: '{}' → '{}'", modelName, entry.getKey());
                return entry.getValue();
            }
        }
        log.warn("未知 model '{}', 使用默认全链 fallback ({} providers)", modelName, defaultFallback.size());
        return defaultFallback;
    }

    public Map<String, List<LlmAdapter>> routes() { return routes; }
}

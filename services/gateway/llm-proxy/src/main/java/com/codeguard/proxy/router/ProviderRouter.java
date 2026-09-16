package com.codeguard.proxy.router;

import com.codeguard.proxy.adapter.LlmAdapter;
import com.codeguard.proxy.config.ProxyConfig;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/** 按模型名称匹配服务商降级链；未匹配时使用已配置适配器组成的默认链。 */
public final class ProviderRouter {
    private static final Logger log = LoggerFactory.getLogger(ProviderRouter.class);

    public record RouteTarget(LlmAdapter adapter, String model) {}

    private final Map<String, List<RouteTarget>> routes;
    private final List<RouteTarget> defaultFallback;

    public ProviderRouter(ProxyConfig config, Map<String, LlmAdapter> adapters) {
        // 根据配置构建模型路由表。
        var routeMap = new java.util.LinkedHashMap<String, List<RouteTarget>>();
        for (var entry : config.routes().entrySet()) {
            String modelName = entry.getKey();
            List<ProxyConfig.RouteTargetConfig> chain = entry.getValue().chain();
            List<RouteTarget> adapterChain = new ArrayList<>();
            for (ProxyConfig.RouteTargetConfig configuredTarget : chain) {
                LlmAdapter adapter = adapters.get(configuredTarget.provider());
                if (adapter != null) {
                    String providerModel = configuredTarget.model().isBlank()
                        ? modelName
                        : configuredTarget.model();
                    adapterChain.add(new RouteTarget(adapter, providerModel));
                } else {
                    log.warn("路由 {} 引用了未知 provider '{}', 已跳过",
                        modelName, configuredTarget.provider());
                }
            }
            if (!adapterChain.isEmpty()) {
                routeMap.put(modelName, adapterChain);
            }
        }
        this.routes = Collections.unmodifiableMap(routeMap);

        // 没有匹配路由时，依次尝试已配置的服务商适配器。
        this.defaultFallback = adapters.values().stream()
            .map(adapter -> new RouteTarget(adapter, ""))
            .toList();

        log.info("路由表已加载: {} 条路由, {} 个 provider",
            routes.size(), adapters.size());
        for (var entry : routes.entrySet()) {
            List<String> names = entry.getValue().stream()
                .map(target -> target.adapter().providerName() + ":" + target.model()).toList();
            log.info("  {} → [{}]", entry.getKey(), String.join(", ", names));
        }
    }

    /**
     * 解析 model 对应的降级链。
     * @return 有序的 adapter 列表（主 → fallback1 → fallback2），未匹配时返回默认全链
     */
    public List<RouteTarget> resolveChain(String modelName) {
        if (modelName == null || modelName.isBlank()) {
            return defaultFallback;
        }
        List<RouteTarget> chain = routes.get(modelName);
        if (chain != null && !chain.isEmpty()) {
            return chain;
        }
        // 模糊匹配：检查已注册模型名是否包含请求的名称。
        for (var entry : routes.entrySet()) {
            if (entry.getKey().contains(modelName) || modelName.contains(entry.getKey())) {
                log.info("模糊匹配: '{}' → '{}'", modelName, entry.getKey());
                return entry.getValue();
            }
        }
        log.warn("未知 model '{}', 使用默认全链 fallback ({} providers)", modelName, defaultFallback.size());
        return defaultFallback.stream()
            .map(target -> new RouteTarget(target.adapter(), modelName))
            .toList();
    }

    public Map<String, List<RouteTarget>> routes() { return routes; }
}

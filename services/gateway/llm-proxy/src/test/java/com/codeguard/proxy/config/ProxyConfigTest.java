package com.codeguard.proxy.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ProxyConfigTest {
    @Test
    void loadsBundledConfigWhenNoExternalFileIsAvailable() {
        ProxyConfig config = ProxyConfig.loadResource("llm-proxy-config.yml");

        assertFalse(config.providers().isEmpty());
        assertTrue(config.providers().containsKey("deepseek"));
        assertEquals(
            java.util.List.of(
                new ProxyConfig.RouteTargetConfig("deepseek", "deepseek-chat"),
                new ProxyConfig.RouteTargetConfig("qwen", "qwen-max")
            ),
            config.routes().get("deepseek-chat").chain()
        );
    }
}

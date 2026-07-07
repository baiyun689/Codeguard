package com.codeguard.agent.repomap;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * TagExtractorRegistry:扩展名路由的边界 —— 大小写、无扩展名、未注册扩展名、覆盖注册。
 */
class TagExtractorRegistryTest {

    @Test
    void defaultsRouteJavaToJavaExtractor() {
        TagExtractorRegistry registry = TagExtractorRegistry.defaults();
        assertTrue(registry.supports("Service.java"));
        assertInstanceOf(JavaTagExtractor.class, registry.forFile("Service.java"));
        assertEquals(java.util.Set.of("java"), registry.supportedExtensions());
    }

    @Test
    void extensionMatchIsCaseInsensitive() {
        TagExtractorRegistry registry = TagExtractorRegistry.defaults();
        assertTrue(registry.supports("Service.JAVA"));
        assertInstanceOf(JavaTagExtractor.class, registry.forFile("Service.Java"));
    }

    @Test
    void unregisteredExtensionUnsupportedAndNull() {
        TagExtractorRegistry registry = TagExtractorRegistry.defaults();
        assertFalse(registry.supports("script.py"));
        assertNull(registry.forFile("script.py"));
    }

    @Test
    void fileWithoutExtensionUnsupported() {
        TagExtractorRegistry registry = TagExtractorRegistry.defaults();
        assertFalse(registry.supports("Makefile"));
        assertFalse(registry.supports("trailingdot."));
        assertNull(registry.forFile("Makefile"));
    }

    @Test
    void registerAddsAndOverridesByExtension() {
        TagExtractorRegistry registry = new TagExtractorRegistry();
        TagExtractor first = (relFile, source) -> List.of();
        TagExtractor second = (relFile, source) -> List.of();

        registry.register("py", first);
        assertTrue(registry.supports("a.py"));
        assertEquals(first, registry.forFile("a.py"));

        // 同扩展名再注册应覆盖(大小写归一)。
        registry.register("PY", second);
        assertEquals(second, registry.forFile("b.py"));
        assertEquals(1, registry.supportedExtensions().size());
    }
}

package com.codeguard.toolserver;

import org.junit.jupiter.api.Test;

import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class GatewaySettingsTest {
    @Test
    void appliesDocumentedDefaults() {
        GatewaySettings settings = GatewaySettings.from(Map.of(), Path.of("tmp"));

        assertEquals(2, settings.maxConcurrentReviews());
        assertEquals(Duration.ofSeconds(600), settings.reviewTimeout());
        assertEquals(Duration.ofSeconds(30), settings.retryDelay());
        assertEquals(Duration.ofSeconds(30), settings.shutdownGrace());
        assertEquals(Path.of("./data/codeguard-jobs"), settings.jobDbPath());
        assertEquals(Path.of("tmp", "codeguard-jobs"), settings.workspaceDir());
    }

    @Test
    void rejectsNonPositiveConcurrencyAtStartup() {
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class,
            () -> GatewaySettings.from(Map.of("CODEGUARD_MAX_CONCURRENT_REVIEWS", "0"), Path.of("tmp")));
        assertTrue(error.getMessage().contains("CODEGUARD_MAX_CONCURRENT_REVIEWS"));
    }
}

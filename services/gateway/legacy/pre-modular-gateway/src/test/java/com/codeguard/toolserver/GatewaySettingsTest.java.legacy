package com.codeguard.toolserver;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class GatewaySettingsTest {
    @TempDir
    Path tempDir;

    @Test
    void appliesDocumentedDefaults() {
        GatewaySettings settings = GatewaySettings.from(Map.of(), Path.of("tmp"));

        assertEquals(2, settings.maxConcurrentReviews());
        assertEquals(Duration.ofSeconds(600), settings.reviewTimeout());
        assertEquals(Duration.ofSeconds(30), settings.retryDelay());
        assertEquals(Duration.ofSeconds(30), settings.shutdownGrace());
        assertEquals(Path.of("tmp", "codeguard-jobs"), settings.workspaceDir());
        assertEquals("jdbc:mysql://localhost:3306/codeguard", settings.jobDbUrl());
        assertEquals("codeguard", settings.jobDbUser());
        assertEquals("codeguard", settings.jobDbPassword());
    }

    @Test
    void rejectsNonPositiveConcurrencyAtStartup() {
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class,
            () -> GatewaySettings.from(Map.of("CODEGUARD_MAX_CONCURRENT_REVIEWS", "0"), Path.of("tmp")));
        assertTrue(error.getMessage().contains("CODEGUARD_MAX_CONCURRENT_REVIEWS"));
    }

    @Test
    void readsGitHubPrivateKeyFromFile() throws IOException {
        Path pem = tempDir.resolve("github-app.pem");
        Files.writeString(pem, "file-key");

        GatewaySettings settings = GatewaySettings.from(Map.of(
            "CODEGUARD_GITHUB_PRIVATE_KEY_FILE", pem.toString()), tempDir);

        assertEquals("file-key", settings.githubPrivateKey());
    }

    @Test
    void privateKeyFileTakesPrecedenceOverInlineValue() throws IOException {
        Path pem = tempDir.resolve("github-app.pem");
        Files.writeString(pem, "file-key");

        GatewaySettings settings = GatewaySettings.from(Map.of(
            "CODEGUARD_GITHUB_PRIVATE_KEY_FILE", pem.toString(),
            "CODEGUARD_GITHUB_PRIVATE_KEY", "inline-key"), tempDir);

        assertEquals("file-key", settings.githubPrivateKey());
    }

    @Test
    void rejectsMissingOrEmptyPrivateKeyFile() throws IOException {
        Path empty = tempDir.resolve("empty.pem");
        Files.writeString(empty, " ");

        assertThrows(IllegalArgumentException.class, () -> GatewaySettings.from(
            Map.of("CODEGUARD_GITHUB_PRIVATE_KEY_FILE", tempDir.resolve("missing.pem").toString()), tempDir));
        assertThrows(IllegalArgumentException.class, () -> GatewaySettings.from(
            Map.of("CODEGUARD_GITHUB_PRIVATE_KEY_FILE", empty.toString()), tempDir));
    }

    @Test
    void keepsInlinePrivateKeyCompatibility() {
        GatewaySettings settings = GatewaySettings.from(
            Map.of("CODEGUARD_GITHUB_PRIVATE_KEY", "inline-key"), tempDir);

        assertEquals("inline-key", settings.githubPrivateKey());
    }
}

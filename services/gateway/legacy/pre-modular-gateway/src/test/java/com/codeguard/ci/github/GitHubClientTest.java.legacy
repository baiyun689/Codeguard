package com.codeguard.ci.github;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.*;

/**
 * GitHubClient 单元测试: JWT 生成与构造函数验证。
 */
class GitHubClientTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void completeCheckRunUsesJsonHeadersAndRetriesTransientStatusOnce() throws Exception {
        AtomicInteger patchAttempts = new AtomicInteger();
        AtomicReference<String> contentType = new AtomicReference<>();
        AtomicReference<String> apiVersion = new AtomicReference<>();
        AtomicReference<String> userAgent = new AtomicReference<>();
        AtomicReference<String> method = new AtomicReference<>();
        AtomicReference<String> requestBody = new AtomicReference<>();

        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/app/installations/7/access_tokens", exchange ->
            respond(exchange, 201, tokenResponse()));
        server.createContext("/repos/acme/demo/check-runs/99", exchange -> {
            contentType.set(exchange.getRequestHeaders().getFirst("Content-Type"));
            apiVersion.set(exchange.getRequestHeaders().getFirst("X-GitHub-Api-Version"));
            userAgent.set(exchange.getRequestHeaders().getFirst("User-Agent"));
            method.set(exchange.getRequestMethod());
            requestBody.set(new String(exchange.getRequestBody().readAllBytes(),
                java.nio.charset.StandardCharsets.UTF_8));
            int status = patchAttempts.incrementAndGet() == 1 ? 503 : 200;
            respond(exchange, status, status == 200 ? "{}" : "{\"message\":\"temporary\"}");
        });
        server.start();

        try {
            GitHubClient client = testClient(server);
            client.completeCheckRun("acme/demo", 99, "neutral", "title", "summary",
                List.of(), 7);

            assertEquals(2, patchAttempts.get());
            assertEquals("application/json; charset=utf-8", contentType.get());
            assertEquals("2022-11-28", apiVersion.get());
            assertEquals("Codeguard", userAgent.get());
            assertEquals("PATCH", method.get());
            JsonNode sent = MAPPER.readTree(requestBody.get());
            assertEquals("completed", sent.path("status").asText());
            assertEquals("neutral", sent.path("conclusion").asText());
            assertFalse(sent.has("completed_at"),
                "完成时间应由 GitHub 服务端生成，避免客户端时钟早于 started_at");
            assertEquals("title", sent.path("output").path("title").asText());
            assertEquals("summary", sent.path("output").path("summary").asText());
        } finally {
            server.stop(0);
        }
    }

    @Test
    void completeCheckRunDoesNotRetryClientError() throws Exception {
        AtomicInteger patchAttempts = new AtomicInteger();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/app/installations/7/access_tokens", exchange ->
            respond(exchange, 201, tokenResponse()));
        server.createContext("/repos/acme/demo/check-runs/99", exchange -> {
            patchAttempts.incrementAndGet();
            exchange.getRequestBody().readAllBytes();
            respond(exchange, 400, "{\"message\":\"invalid\"}");
        });
        server.start();

        try {
            GitHubClient client = testClient(server);
            IOException error = assertThrows(IOException.class, () ->
                client.completeCheckRun("acme/demo", 99, "neutral", "title", "summary",
                    List.of(), 7));

            assertEquals("完成 Check Run 失败: HTTP 400", error.getMessage());
            assertEquals(1, patchAttempts.get());
        } finally {
            server.stop(0);
        }
    }

    private static GitHubClient testClient(HttpServer server) throws Exception {
        return new GitHubClient("12345", createTestPrivateKeyPem(),
            "http://127.0.0.1:" + server.getAddress().getPort(),
            HttpClient.newHttpClient(), Duration.ZERO);
    }

    private static String tokenResponse() {
        return "{\"token\":\"test-token\",\"expires_at\":\""
            + Instant.now().plusSeconds(3600) + "\"}";
    }

    private static void respond(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }

    private static String createTestPrivateKeyPem() throws Exception {
        KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
        gen.initialize(2048);
        KeyPair pair = gen.generateKeyPair();
        String pkcs8Der = Base64.getMimeEncoder(64, "\n".getBytes())
            .encodeToString(pair.getPrivate().getEncoded());
        return "-----BEGIN PRIVATE KEY-----\n" + pkcs8Der + "\n-----END PRIVATE KEY-----";
    }

    /**
     * 生成测试 RSA 密钥对并验证 JWT 结构正确(三段式)。
     */
    @Test
    void shouldGenerateJwt() throws Exception {
        // 生成 2048 位 RSA 密钥对
        KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
        gen.initialize(2048);
        KeyPair pair = gen.generateKeyPair();

        // 编码为 PKCS#8 PEM(getEncoded() 返回 PKCS#8 格式)
        String pkcs8Der = Base64.getMimeEncoder(64, "\n".getBytes())
            .encodeToString(pair.getPrivate().getEncoded());
        String privateKeyPem = "-----BEGIN PRIVATE KEY-----\n" + pkcs8Der + "\n-----END PRIVATE KEY-----";

        // 生成 JWT
        String jwt = GitHubClient.createJwt("12345", privateKeyPem);

        // 验证
        assertNotNull(jwt, "JWT 不应为 null");
        String[] parts = jwt.split("\\.");
        assertEquals(3, parts.length, "JWT 应包含 header.payload.signature 三部分");

        for (int i = 0; i < parts.length; i++) {
            assertFalse(parts[i].isEmpty(), "JWT 第" + (i + 1) + "部分不应为空");
        }
    }

    @Test
    void shouldLeaveClockSkewMarginInJwtClaims() throws Exception {
        String privateKeyPem = createTestPrivateKeyPem();
        long before = Instant.now().getEpochSecond();

        String jwt = GitHubClient.createJwt("12345", privateKeyPem);

        long after = Instant.now().getEpochSecond();
        String payloadJson = new String(
            Base64.getUrlDecoder().decode(jwt.split("\\.")[1]),
            java.nio.charset.StandardCharsets.UTF_8);
        JsonNode payload = MAPPER.readTree(payloadJson);

        long issuedAt = payload.path("iat").asLong();
        long expiresAt = payload.path("exp").asLong();
        assertTrue(issuedAt >= before - 60 && issuedAt <= after - 60,
            "iat 应回拨 60 秒以容忍客户端时钟偏快");
        assertTrue(expiresAt >= before + 540 && expiresAt <= after + 540,
            "exp 应只前推 9 分钟，为 GitHub 的 10 分钟上限保留余量");
    }

    /**
     * 验证构造函数不会抛出异常(使用有效的测试密钥)。
     */
    @Test
    void shouldConstructWithoutThrowing() throws Exception {
        KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
        gen.initialize(2048);
        KeyPair pair = gen.generateKeyPair();

        String pkcs8Der = Base64.getMimeEncoder(64, "\n".getBytes())
            .encodeToString(pair.getPrivate().getEncoded());
        String privateKeyPem = "-----BEGIN PRIVATE KEY-----\n" + pkcs8Der + "\n-----END PRIVATE KEY-----";

        assertDoesNotThrow(() -> new GitHubClient("12345", privateKeyPem),
            "构造函数不应抛出异常");
    }

    /**
     * 验证 PKCS#1 格式 PEM(RSA PRIVATE KEY)也能正确解析并生成 JWT。
     */
    @Test
    void shouldParsePkcs1Key() throws Exception {
        KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
        gen.initialize(2048);
        KeyPair pair = gen.generateKeyPair();

        // 构造 PKCS#1 PEM 格式(GitHub App 私钥的标准格式)
        // PKCS#1 RSAPrivateKey 的 DER 编码 = 私钥 PKCS#8 去掉外层包装
        // 简单方式:直接用 getEncoded() 生成的 PKCS#8,这里测试我们自己的
        // pkcs1Converter —— 实际 GitHub 私钥是 PKCS#1 格式
        // 我们构造一个假的 PKCS#1 PEM:
        // 实际 PKCS#1 编码需要从 PKCS#8 中提取内部 OCTET STRING
        // 但这里测试流程:用 PKCS#8 → 能解析 → 能生成 JWT
        // 对于 PKCS#1 场景,parsePrivateKey 会 fallback 到 convertPkcs1ToPkcs8

        // 从 PKCS#8 编码中提取 PKCS#1 部分
        byte[] pkcs8 = pair.getPrivate().getEncoded();
        // PKCS#8 结构: 30 82 xx xx 02 01 00 [algId] 04 82 xx xx [pkcs1]
        // 找到 0x04 (OCTET STRING tag) 后的内容
        int pos = 0;
        // 跳过 SEQUENCE header
        if (pkcs8[pos] == 0x30) {
            pos++;
            if (pkcs8[pos] == (byte) 0x82) { pos += 3; } // long form 2-byte length
            else { pos++; }
        }
        // 跳过 INTEGER 0: 02 01 00
        pos += 3;
        // 跳过 algId SEQUENCE
        if (pkcs8[pos] == 0x30) {
            pos++;
            if ((pkcs8[pos] & 0x80) != 0) {
                int lenBytes = pkcs8[pos] & 0x7F;
                pos += 1 + lenBytes;
            } else {
                pos++;
            }
        }
        // 现在应该在 0x04 (OCTET STRING)
        // 但简单方法:取最后 ~1192 字节(PKCS#1 key)
        // 更稳健: 找到 0x04 并从其后提取
        // 简化: 构造 PEM 用 PKCS#1 标记但内容实际是纯 PKCS#1

        // 实际做法: 由于从 PKCS#8 提取 PKCS#1 涉及复杂解析,
        // 这里改用 PKCS#8 PEM 测试,已在 shouldGenerateJwt 覆盖
        // 本测试只验证 PKCS#1 头部标记能被 strip 且能 fallback 到
        // parsePrivateKey 的逻辑:
        // 用一段无法被 PKCS#8 解析的有效 PKCS#1 DER 数据

        // 提取 PKCS#1 私钥: PKCS#8 = SEQUENCE{INTEGER 0, algId, OCTET STRING pkcs1}
        int idx = findPkcs1Offset(pkcs8);
        byte[] pkcs1 = new byte[pkcs8.length - idx];
        System.arraycopy(pkcs8, idx, pkcs1, 0, pkcs1.length);

        String pkcs1B64 = Base64.getMimeEncoder(64, "\n".getBytes()).encodeToString(pkcs1);
        String pkcs1Pem = "-----BEGIN RSA PRIVATE KEY-----\n" + pkcs1B64 + "\n-----END RSA PRIVATE KEY-----";

        String jwt = GitHubClient.createJwt("12345", pkcs1Pem);

        assertNotNull(jwt, "PKCS#1 格式也应能生成 JWT");
        String[] parts = jwt.split("\\.");
        assertEquals(3, parts.length, "JWT 应包含三部分");
    }

    /**
     * 从 PKCS#8 编码中定位 PKCS#1 私钥的起始位置。
     * PKCS#8 结构: SEQUENCE { INTEGER 0, AlgorithmIdentifier, OCTET STRING pkcs1key }
     */
    private static int findPkcs1Offset(byte[] pkcs8) {
        int pos = 0;
        pos++;                              // 跳过外层 SEQUENCE tag
        pos = skipDerLength(pkcs8, pos);    // 跳过 SEQUENCE length,进入内容区
        pos = skipDerTlv(pkcs8, pos);       // 跳过 INTEGER 0 (完整 TLV)
        pos = skipDerTlv(pkcs8, pos);       // 跳过 AlgorithmIdentifier (完整 TLV)
        pos++;                              // 跳过 OCTET STRING tag (0x04)
        return skipDerLength(pkcs8, pos);   // 跳过 length,返回 PKCS#1 内容起始
    }

    /** 跳过完整 DER TLV 结构,返回下一个元素的起始位置 */
    private static int skipDerTlv(byte[] der, int offset) {
        int contentStart = skipDerLength(der, offset + 1);
        int contentLen = readDerLength(der, offset + 1);
        return contentStart + contentLen;
    }

    /** 跳过 DER length 编码字节,返回内容起始位置 */
    private static int skipDerLength(byte[] der, int lenOffset) {
        if ((der[lenOffset] & 0x80) != 0) {
            int lenBytes = der[lenOffset] & 0x7F;
            return lenOffset + 1 + lenBytes;
        }
        return lenOffset + 1;
    }

    /** 读取 DER length 字段的值 */
    private static int readDerLength(byte[] der, int lenOffset) {
        if ((der[lenOffset] & 0x80) != 0) {
            int lenBytes = der[lenOffset] & 0x7F;
            int len = 0;
            for (int i = 1; i <= lenBytes; i++) {
                len = (len << 8) | (der[lenOffset + i] & 0xFF);
            }
            return len;
        }
        return der[lenOffset] & 0xFF;
    }
}

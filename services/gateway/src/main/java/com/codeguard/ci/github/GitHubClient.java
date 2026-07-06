package com.codeguard.ci.github;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.KeyFactory;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.PKCS8EncodedKeySpec;
import java.time.Instant;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * GitHub API 客户端: App 安装令牌管理 + Check Runs API + PR 行内评论。
 * 使用 Java 11 内置 HttpClient,无需额外依赖。
 */
public class GitHubClient {

    private static final Logger log = LoggerFactory.getLogger(GitHubClient.class);
    private static final String API_BASE = "https://api.github.com";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** RSA 算法标识符 DER 编码(固定 15 字节),用于 PKCS#1 → PKCS#8 转换 */
    private static final byte[] RSA_ALG_ID = {
        0x30, 0x0D, 0x06, 0x09, 0x2A, (byte) 0x86, 0x48, (byte) 0x86, (byte) 0xF7, 0x0D,
        0x01, 0x01, 0x01, 0x05, 0x00
    };

    private final String appId;
    private final String privateKeyPem;
    private final PrivateKey privateKey;
    private final HttpClient httpClient;
    private final Map<Long, TokenCache> tokenCache = new ConcurrentHashMap<>();

    /**
     * Check Run 标注信息: 文件路径、行号、级别、说明。
     */
    public record IssueAnnot(String path, int line, String annotationLevel, String message) {}

    /** 安装令牌缓存: token + 过期时间 */
    private record TokenCache(String token, Instant expiresAt) {}

    /**
     * @param appId         GitHub App ID
     * @param privateKeyPem RSA 私钥 PEM(PKCS#1 或 PKCS#8 格式)
     */
    public GitHubClient(String appId, String privateKeyPem) {
        this.appId = appId;
        this.privateKeyPem = privateKeyPem;
        this.privateKey = parsePrivateKey(privateKeyPem);
        this.httpClient = HttpClient.newHttpClient();
    }

    // ── JWT 生成 ──

    /**
     * 生成 GitHub App 认证 JWT(RS256)。
     * 供单元测试直接调用; 实例方法通过内部缓存字段复用。
     *
     * @param appId         GitHub App ID
     * @param privateKeyPem RSA 私钥 PEM
     * @return 三段式 JWT 字符串(header.payload.signature)
     */
    static String createJwt(String appId, String privateKeyPem) throws Exception {
        PrivateKey key = parsePrivateKey(privateKeyPem);

        String headerJson = "{\"alg\":\"RS256\",\"typ\":\"JWT\"}";
        String header = base64url(headerJson.getBytes());

        long now = Instant.now().getEpochSecond();
        String payloadJson = "{\"iat\":" + now + ",\"exp\":" + (now + 600) + ",\"iss\":\"" + appId + "\"}";
        String payload = base64url(payloadJson.getBytes());

        String signingInput = header + "." + payload;
        Signature sig = Signature.getInstance("SHA256withRSA");
        sig.initSign(key);
        sig.update(signingInput.getBytes());
        String signature = base64url(sig.sign());

        return signingInput + "." + signature;
    }

    private String createJwt() throws Exception {
        return createJwt(appId, privateKeyPem);
    }

    private static String base64url(byte[] data) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(data);
    }

    // ── PEM 解析 ──

    /**
     * 解析 PEM 格式 RSA 私钥,自动识别 PKCS#1 / PKCS#8。
     */
    private static PrivateKey parsePrivateKey(String pem) {
        String key = pem
            .replace("-----BEGIN RSA PRIVATE KEY-----", "")
            .replace("-----END RSA PRIVATE KEY-----", "")
            .replace("-----BEGIN PRIVATE KEY-----", "")
            .replace("-----END PRIVATE KEY-----", "")
            .replaceAll("\\s", "");
        byte[] decoded = Base64.getDecoder().decode(key);

        try {
            // 先尝试 PKCS#8
            KeyFactory kf = KeyFactory.getInstance("RSA");
            return kf.generatePrivate(new PKCS8EncodedKeySpec(decoded));
        } catch (Exception e) {
            // PKCS#1 → PKCS#8 转换
            try {
                byte[] pkcs8 = convertPkcs1ToPkcs8(decoded);
                KeyFactory kf = KeyFactory.getInstance("RSA");
                return kf.generatePrivate(new PKCS8EncodedKeySpec(pkcs8));
            } catch (Exception ex) {
                throw new RuntimeException("无法解析 RSA 私钥", ex);
            }
        }
    }

    /**
     * 将 PKCS#1 RSAPrivateKey 包装为 PKCS#8 PrivateKeyInfo。
     */
    private static byte[] convertPkcs1ToPkcs8(byte[] pkcs1) throws Exception {
        byte[] octetString = wrapDer((byte) 0x04, pkcs1);

        int innerLen = 3 + RSA_ALG_ID.length + octetString.length; // INTEGER 0 + algId + octetString
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(0x30); // SEQUENCE
        writeDerLength(out, innerLen);
        out.write(new byte[] {0x02, 0x01, 0x00}); // INTEGER 0 (version)
        out.write(RSA_ALG_ID);
        out.write(octetString);

        return out.toByteArray();
    }

    private static byte[] wrapDer(byte tag, byte[] data) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(tag);
        writeDerLength(out, data.length);
        out.write(data);
        return out.toByteArray();
    }

    private static void writeDerLength(ByteArrayOutputStream out, int len) throws IOException {
        if (len < 128) {
            out.write(len);
        } else if (len < 256) {
            out.write(new byte[] {(byte) 0x81, (byte) len});
        } else {
            out.write(new byte[] {(byte) 0x82, (byte) (len >> 8), (byte) len});
        }
    }

    // ── 安装令牌 ──

    /**
     * 获取 GitHub App 安装令牌,带缓存(60s 过期 buffer)。
     */
    public String getInstallationToken(long installationId) throws IOException, InterruptedException {
        TokenCache cached = tokenCache.get(installationId);
        if (cached != null && Instant.now().plusSeconds(60).isBefore(cached.expiresAt)) {
            return cached.token;
        }

        String jwt;
        try {
            jwt = createJwt();
        } catch (Exception e) {
            throw new IOException("JWT 生成失败", e);
        }

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/app/installations/" + installationId + "/access_tokens"))
            .header("Authorization", "Bearer " + jwt)
            .header("Accept", "application/vnd.github+json")
            .POST(HttpRequest.BodyPublishers.noBody())
            .build();

        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 201) {
            log.error("获取 installation token 失败: HTTP {} {}", response.statusCode(), response.body());
            throw new IOException("获取 installation token 失败: HTTP " + response.statusCode());
        }

        JsonNode node = MAPPER.readTree(response.body());
        String token = node.get("token").asText();
        Instant expiresAt = Instant.parse(node.get("expires_at").asText());

        tokenCache.put(installationId, new TokenCache(token, expiresAt));
        log.info("已获取 installation token (installation={}), 过期时间: {}", installationId, expiresAt);
        return token;
    }

    // ── Check Runs ──

    /**
     * 创建 Check Run,状态设为 in_progress。
     *
     * @return check_run id
     */
    public long createCheckRun(String repo, String headSha, long installationId)
            throws IOException, InterruptedException {
        String token = getInstallationToken(installationId);

        ObjectNode body = MAPPER.createObjectNode();
        body.put("name", "Codeguard Review");
        body.put("head_sha", headSha);
        body.put("status", "in_progress");

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/repos/" + repo + "/check-runs"))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/vnd.github+json")
            .POST(HttpRequest.BodyPublishers.ofString(body.toString()))
            .build();

        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 201) {
            log.error("创建 Check Run 失败: HTTP {} {}", response.statusCode(), response.body());
            throw new IOException("创建 Check Run 失败: HTTP " + response.statusCode());
        }

        JsonNode node = MAPPER.readTree(response.body());
        long checkRunId = node.get("id").asLong();
        log.info("已创建 Check Run: id={} repo={} sha={}", checkRunId, repo, headSha);
        return checkRunId;
    }

    /**
     * 完成 Check Run,附带审查结论、摘要与标注列表。
     */
    public void completeCheckRun(String repo, long checkRunId, String conclusion,
                                  String title, String summary,
                                  List<IssueAnnot> annotations,
                                  long installationId) throws IOException, InterruptedException {
        String token = getInstallationToken(installationId);

        ObjectNode body = MAPPER.createObjectNode();
        body.put("name", "Codeguard Review");
        body.put("status", "completed");
        body.put("conclusion", conclusion);

        ObjectNode output = MAPPER.createObjectNode();
        output.put("title", title);
        output.put("summary", summary);

        if (annotations != null && !annotations.isEmpty()) {
            ArrayNode annots = MAPPER.createArrayNode();
            for (IssueAnnot a : annotations) {
                ObjectNode annot = MAPPER.createObjectNode();
                annot.put("path", a.path);
                annot.put("start_line", a.line);
                annot.put("end_line", a.line);
                annot.put("annotation_level", a.annotationLevel);
                annot.put("message", a.message);
                annots.add(annot);
            }
            output.set("annotations", annots);
        }

        body.set("output", output);

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/repos/" + repo + "/check-runs/" + checkRunId))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/vnd.github+json")
            .method("PATCH", HttpRequest.BodyPublishers.ofString(body.toString()))
            .build();

        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            log.error("完成 Check Run 失败: HTTP {} {}", response.statusCode(), response.body());
            throw new IOException("完成 Check Run 失败: HTTP " + response.statusCode());
        }

        log.info("已更新 Check Run: id={} conclusion={}", checkRunId, conclusion);
    }

    // ── PR 行内评论 ──

    /**
     * 在 PR 指定文件的指定行创建行内评论。
     */
    public void createPRComment(String repo, int prNumber, String commitId,
                                 String path, int line, String body,
                                 long installationId) throws IOException, InterruptedException {
        String token = getInstallationToken(installationId);

        ObjectNode commentBody = MAPPER.createObjectNode();
        commentBody.put("body", body);
        commentBody.put("commit_id", commitId);
        commentBody.put("path", path);
        commentBody.put("line", line);

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(API_BASE + "/repos/" + repo + "/pulls/" + prNumber + "/comments"))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/vnd.github+json")
            .POST(HttpRequest.BodyPublishers.ofString(commentBody.toString()))
            .build();

        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 201) {
            log.error("创建 PR 评论失败: HTTP {} {}", response.statusCode(), response.body());
            throw new IOException("创建 PR 评论失败: HTTP " + response.statusCode());
        }

        log.info("已创建 PR 评论: repo={} PR#{} path={}:{}", repo, prNumber, path, line);
    }
}

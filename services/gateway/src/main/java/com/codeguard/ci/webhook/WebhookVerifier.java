package com.codeguard.ci.webhook;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.security.MessageDigest;
import java.util.HexFormat;

/**
 * GitHub webhook HMAC-SHA256 验签。
 * 使用 constant-time 比较防止时序攻击。
 */
public final class WebhookVerifier {

    private final byte[] secret;

    public WebhookVerifier(String secret) {
        this.secret = secret.getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }

    /**
     * 验证 X-Hub-Signature-256 头。
     * @param signatureHeader  "sha256=&lt;hex&gt;" 或 null
     * @param body             webhook 原始请求体
     * @return true 验证通过
     */
    public boolean verify(String signatureHeader, byte[] body) {
        if (signatureHeader == null || !signatureHeader.startsWith("sha256=")) {
            return false;
        }
        String expected = signatureHeader.substring(7);
        String computed = hmacSha256(body);
        return MessageDigest.isEqual(
            expected.getBytes(java.nio.charset.StandardCharsets.UTF_8),
            computed.getBytes(java.nio.charset.StandardCharsets.UTF_8)
        );
    }

    private String hmacSha256(byte[] data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(data));
        } catch (Exception e) {
            throw new RuntimeException("HMAC-SHA256 计算失败", e);
        }
    }
}

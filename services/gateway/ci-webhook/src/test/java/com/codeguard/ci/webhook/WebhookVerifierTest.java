package com.codeguard.ci.webhook;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class WebhookVerifierTest {

    private final WebhookVerifier verifier = new WebhookVerifier("test_secret");

    @Test
    void shouldPassWithCorrectSignature() throws Exception {
        byte[] body = "{\"action\":\"opened\"}".getBytes();
        String sig = computeSignature("test_secret", body);
        assertTrue(verifier.verify(sig, body));
    }

    @Test
    void shouldRejectWrongSecret() throws Exception {
        byte[] body = "{\"action\":\"opened\"}".getBytes();
        String sig = computeSignature("wrong_secret", body);
        assertFalse(verifier.verify(sig, body));
    }

    @Test
    void shouldRejectNullHeader() {
        assertFalse(verifier.verify(null, "{}".getBytes()));
    }

    @Test
    void shouldRejectTamperedBody() throws Exception {
        byte[] original = "{\"action\":\"opened\"}".getBytes();
        String sig = computeSignature("test_secret", original);
        byte[] tampered = "{\"action\":\"closed\"}".getBytes();
        assertFalse(verifier.verify(sig, tampered));
    }

    private String computeSignature(String secret, byte[] body) throws Exception {
        javax.crypto.Mac mac = javax.crypto.Mac.getInstance("HmacSHA256");
        mac.init(new javax.crypto.spec.SecretKeySpec(
            secret.getBytes(java.nio.charset.StandardCharsets.UTF_8), "HmacSHA256"));
        return "sha256=" + java.util.HexFormat.of().formatHex(mac.doFinal(body));
    }
}

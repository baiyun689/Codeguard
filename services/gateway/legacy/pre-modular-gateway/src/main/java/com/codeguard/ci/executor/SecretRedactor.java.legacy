package com.codeguard.ci.executor;

final class SecretRedactor {
    private SecretRedactor() {}

    static String redact(String text, String secret) {
        if (text == null || secret == null || secret.isBlank()) return text;
        return text.replace(secret, "[REDACTED]");
    }
}

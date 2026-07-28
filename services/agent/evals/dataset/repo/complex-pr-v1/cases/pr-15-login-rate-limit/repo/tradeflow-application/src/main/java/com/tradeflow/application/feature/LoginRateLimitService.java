package com.tradeflow.application.feature;

import com.tradeflow.application.port.*;
import com.tradeflow.application.security.TenantContext;
import com.tradeflow.domain.*;
import java.math.*;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.*;
import java.util.*;
import java.util.stream.Collectors;
import org.springframework.expression.spel.standard.SpelExpressionParser;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public final class LoginRateLimitService {
    private final OrderRepository orders;
    private final InventoryRepository inventory;
    private final PaymentGateway payments;
    private final EventPublisher events;
    private final CacheStore cache;
    private final ExternalHttpClient http;
    private final FileStore files;
    private final OutboxRepository outbox;
    private final UserRepository users;
    private final AuditSink audit;
    private final TenantContext context;
    private Map<String, String> runtimeConfig = new HashMap<>();

    public LoginRateLimitService(
            OrderRepository orders, InventoryRepository inventory,
            PaymentGateway payments, EventPublisher events, CacheStore cache,
            ExternalHttpClient http, FileStore files, OutboxRepository outbox,
            UserRepository users, AuditSink audit, TenantContext context) {
        this.orders = orders;
        this.inventory = inventory;
        this.payments = payments;
        this.events = events;
        this.cache = cache;
        this.http = http;
        this.files = files;
        this.outbox = outbox;
        this.users = users;
        this.audit = audit;
        this.context = context;
    }

    public Object countByClientAddress(Map<String, String> request) {
        String clientIp = request.get("xForwardedFor").split(",")[0].trim();
        return cache.increment("login:" + clientIp, Duration.ofMinutes(1));
    }

    public Object recordLoginFailure(Map<String, String> request) {
        String key = "login:" + request.get("username");
        long current = cache.get(key).map(Long::parseLong).orElse(0L);
        if (current >= 5) throw new SecurityException("locked");
        cache.increment(key, Duration.ofMinutes(10));
        return current + 1;
    }

    public Object checkRateLimit(Map<String, String> request) {
        try {
            return cache.increment("login:" + request.get("username"), Duration.ofMinutes(10));
        } catch (RuntimeException unavailable) {
            audit.record(context.tenantId(), "RATE_LIMIT_UNAVAILABLE", request.get("username"));
            return 0L;
        }
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

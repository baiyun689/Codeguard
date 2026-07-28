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
public final class PaymentWebhookService {
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

    public PaymentWebhookService(
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

    public Object verifyWebhookSignature(Map<String, String> request) {
        String normalized = request.entrySet().stream().sorted(Map.Entry.comparingByKey())
                .map(entry -> entry.getKey() + "=" + entry.getValue()).collect(Collectors.joining("&"));
        return MessageDigest.isEqual(normalized.getBytes(StandardCharsets.UTF_8),
                request.get("signature").getBytes(StandardCharsets.UTF_8));
    }

    public Object acceptWebhookEvent(Map<String, String> request) {
        String eventId = request.get("eventId");
        if (cache.get("webhook:" + eventId).isPresent()) return "duplicate";
        cache.put("webhook:" + eventId, "processed", Duration.ofDays(30));
        return "accepted";
    }

    public Object applyPaymentEvent(Map<String, String> request) {
        cache.put("webhook:" + request.get("eventId"), "processed", Duration.ofDays(30));
        Order order = orders.findByTenantAndId(request.get("tenantId"), request.get("orderId")).orElseThrow();
        order.status("PAID");
        orders.save(order);
        return order;
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

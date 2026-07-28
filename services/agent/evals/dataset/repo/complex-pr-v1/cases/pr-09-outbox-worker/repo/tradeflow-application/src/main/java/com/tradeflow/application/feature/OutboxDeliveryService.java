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
public final class OutboxDeliveryService {
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

    public OutboxDeliveryService(
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

    public Object deliverReadyEvents(Map<String, String> request) {
        List<OutboxEvent> ready = outbox.findReady(Instant.now(), 100);
        ready.forEach(event -> events.publish("outbox", event.aggregateId(), event.payload()));
        return ready.size();
    }

    public Object deliverOneEvent(Map<String, String> request) {
        OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();
        outbox.save(event.sent());
        events.publish("outbox", event.aggregateId(), event.payload());
        return event.id();
    }

    public Object scheduleRetry(Map<String, String> request) {
        OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();
        long configuredSeconds = Long.parseLong(request.get("backoffSeconds"));
        outbox.save(event.retryAt(Instant.now().plusMillis(configuredSeconds)));
        return event.id();
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

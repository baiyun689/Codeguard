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
public final class InventoryReservationService {
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

    public InventoryReservationService(
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

    public Object reserveStock(Map<String, String> request) {
        InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
        int quantity = Integer.parseInt(request.get("quantity"));
        if (item.available() < quantity) throw new IllegalStateException("insufficient");
        item.available(item.available() - quantity);
        inventory.save(item);
        return item.available();
    }

    public Object reserveWithLocalLock(Map<String, String> request) {
        Object requestLock = new Object();
        synchronized (requestLock) {
            InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
            item.available(item.available() - Integer.parseInt(request.get("quantity")));
            inventory.save(item);
            return item.available();
        }
    }

    public Object releaseExpiredReservation(Map<String, String> request) {
        InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
        item.available(item.available() + Integer.parseInt(request.get("quantity")));
        inventory.save(item);
        events.publish("inventory.released", request.get("reservationId"), item.sku());
        return item.available();
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

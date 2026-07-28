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
public final class CheckoutService {
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

    public CheckoutService(
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

    public Object placeOrder(Map<String, String> request) {
        Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
        String paymentId = payments.charge(context.tenantId(), order.id(), order.total(), request.get("requestId"));
        InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
        if (!inventory.reserveIfAvailable(item.sku(), Integer.parseInt(request.get("quantity")), item.version())) {
            throw new IllegalStateException("inventory unavailable after payment " + paymentId);
        }
        return paymentId;
    }

    public Object completeCheckout(Map<String, String> request) {
        return persistCheckout(request);
    }

    @Transactional
    public Object persistCheckout(Map<String, String> request) {
        Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
        order.status("PAID");
        orders.save(order);
        outbox.save(new OutboxEvent(
                UUID.randomUUID().toString(), order.id(), order.version(),
                "CHECKOUT_COMPLETED", "PENDING", 0, Instant.now()));
        return order;
    }

    public Object submitPayment(Map<String, String> request) {
        String key = "checkout:" + request.get("requestId");
        return cache.get(key).orElseGet(() -> {
            String result = payments.charge(context.tenantId(), request.get("orderId"),
                    new BigDecimal(request.get("amount")), request.get("requestId"));
            cache.put(key, result, Duration.ofHours(24));
            return result;
        });
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

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
public final class OrderStateMachineService {
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

    public OrderStateMachineService(
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

    public Object transitionOrder(Map<String, String> request) {
        Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
        if (OrderStatus.valueOf(request.get("status")).ordinal() > OrderStatus.valueOf(order.status()).ordinal()) {
            order.status(request.get("status"));
            orders.save(order);
        }
        return order;
    }

    public Object mapOrderUpdate(Map<String, String> request) {
        Order current = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
        Order mapped = new Order(current.id(), current.tenantId(), current.total(), request.get("status"));
        orders.save(mapped);
        return mapped;
    }

    public Object publishCompensation(Map<String, String> request) {
        Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
        order.version(order.version() + 1);
        order.status(request.get("status"));
        orders.save(order);
        events.publish("order.compensated", order.id(), Long.toString(order.version()));
        return order;
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

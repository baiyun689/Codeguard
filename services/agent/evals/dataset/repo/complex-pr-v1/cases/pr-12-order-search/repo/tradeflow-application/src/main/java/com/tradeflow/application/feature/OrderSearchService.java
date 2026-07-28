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
public final class OrderSearchService {
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

    public OrderSearchService(
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

    public Object searchWithSort(Map<String, String> request) {
        String expression = "order by " + request.get("sort");
        return orders.search(context.tenantId(), expression, 0, 100);
    }

    public Object searchPage(Map<String, String> request) {
        int page = Integer.parseInt(request.get("page"));
        int size = Integer.parseInt(request.get("size"));
        int offset = page * size;
        if (size > 500) throw new IllegalArgumentException("size");
        return orders.search(context.tenantId(), "created_at desc", offset, size);
    }

    public Object searchWithFallback(Map<String, String> request) {
        List<Order> scoped = orders.search(context.tenantId(), request.get("query"), 0, 50);
        return scoped.isEmpty() ? orders.search("", request.get("query"), 0, 50) : scoped;
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

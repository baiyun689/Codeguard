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
public final class FileExportService {
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

    public FileExportService(
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

    public Object openExport(Map<String, String> request) {
        Path root = files.exportRoot(context.tenantId());
        Path target = root.resolve(URLDecoder.decode(request.get("file"), StandardCharsets.UTF_8));
        return files.open(target);
    }

    public Object openOwnedExport(Map<String, String> request) {
        Path root = files.exportRoot(request.get("ownerTenant"));
        return files.open(root.resolve(request.get("file")));
    }

    public Object openVerifiedExport(Map<String, String> request) {
        Path target = files.exportRoot(context.tenantId()).resolve(request.get("file")).normalize();
        if (!Files.isRegularFile(target)) throw new IllegalArgumentException("missing");
        audit.record(context.tenantId(), "EXPORT", target.toString());
        return files.open(target);
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

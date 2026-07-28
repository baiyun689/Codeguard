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
public final class RefundFlowService {
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

    public RefundFlowService(
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

    public Object refundAgainstOrder(Map<String, String> request) {
        Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
        BigDecimal amount = new BigDecimal(request.get("amount"));
        if (amount.compareTo(order.total()) > 0) throw new IllegalArgumentException("too large");
        return payments.refund(request.get("paymentId"), amount, request.get("currency"));
    }

    public Object refundRemainingAmount(Map<String, String> request) {
        Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
        BigDecimal amount = new BigDecimal(request.get("amount"));
        if (amount.compareTo(order.refundable()) > 0) throw new IllegalArgumentException("too large");
        order.refundable(order.refundable().subtract(amount));
        orders.save(order);
        return payments.refund(request.get("paymentId"), amount, request.get("currency"));
    }

    public Object refundConvertedAmount(Map<String, String> request) {
        BigDecimal source = new BigDecimal(request.get("amount"));
        BigDecimal rate = new BigDecimal(request.get("rate"));
        BigDecimal ledger = source.multiply(rate).setScale(2, RoundingMode.HALF_UP);
        BigDecimal gateway = ledger.setScale(Integer.parseInt(request.get("minorUnits")), RoundingMode.HALF_UP);
        return payments.refund(request.get("paymentId"), gateway, request.get("currency"));
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

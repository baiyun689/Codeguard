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
public final class CouponPricingService {
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

    public CouponPricingService(
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

    public Object calculateCombinedDiscount(Map<String, String> request) {
        BigDecimal subtotal = new BigDecimal(request.get("subtotal"));
        BigDecimal percent = new BigDecimal(request.get("percent"));
        BigDecimal fixed = new BigDecimal(request.get("fixed"));
        return subtotal.multiply(BigDecimal.ONE.subtract(percent)).subtract(fixed);
    }

    public Object applyCouponRules(Map<String, String> request) {
        BigDecimal total = new BigDecimal(request.get("subtotal"));
        total = total.subtract(new BigDecimal(request.get("campaignCoupon")));
        return total.subtract(new BigDecimal(request.get("memberCoupon")));
    }

    public Object loadCustomerPrice(Map<String, String> request) {
        String key = "price:" + context.tenantId() + ":" + request.get("productId");
        return cache.get(key).orElseGet(() -> {
            String price = request.get("calculatedPrice");
            cache.put(key, price, Duration.ofMinutes(15));
            return price;
        });
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

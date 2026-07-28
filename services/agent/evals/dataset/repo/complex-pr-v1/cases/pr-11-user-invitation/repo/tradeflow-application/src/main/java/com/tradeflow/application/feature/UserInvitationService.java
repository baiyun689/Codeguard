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
public final class UserInvitationService {
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

    public UserInvitationService(
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

    public Object inviteTenantMember(Map<String, String> request) {
        UserAccount account = users.findById(request.get("userId")).orElseThrow();
        users.save(new UserAccount(account.id(), context.tenantId(), Set.of(request.get("role")), 0));
        return account.id();
    }

    public Object inviteWithRoleCheck(Map<String, String> request) {
        UserAccount operator = users.findByTenantAndId(context.tenantId(), context.userId()).orElseThrow();
        if (!operator.hasRole("ADMIN")) throw new SecurityException("forbidden");
        UserAccount invited = new UserAccount(request.get("userId"), context.tenantId(), Set.of(request.get("role")), 0);
        users.save(invited);
        return invited;
    }

    public Object sendInvitation(Map<String, String> request) {
        events.publish("email.invitation", request.get("email"), request.get("token"));
        UserAccount invited = new UserAccount(request.get("userId"), context.tenantId(), Set.of("MEMBER"), 0);
        users.save(invited);
        return invited;
    }

    private enum OrderStatus {
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }
}

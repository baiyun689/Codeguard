package com.tradeflow;

import com.tradeflow.application.port.*;
import com.tradeflow.application.security.TenantContext;
import com.tradeflow.domain.OutboxEvent;
import com.tradeflow.domain.UserAccount;
import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.math.BigDecimal;
import java.net.URI;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class LocalAdaptersConfiguration {
    @Bean
    CacheStore cacheStore() {
        return new CacheStore() {
            private final Map<String, String> values = new ConcurrentHashMap<>();
            public Optional<String> get(String key) { return Optional.ofNullable(values.get(key)); }
            public void put(String key, String value, Duration ttl) { values.put(key, value); }
            public void evict(String key) { values.remove(key); }
            public long increment(String key, Duration ttl) {
                return Long.parseLong(values.merge(key, "1",
                        (oldValue, ignored) -> Long.toString(Long.parseLong(oldValue) + 1)));
            }
        };
    }

    @Bean
    PaymentGateway paymentGateway() {
        return new PaymentGateway() {
            public String charge(String tenantId, String orderId, BigDecimal amount, String key) {
                return "payment-" + tenantId + "-" + orderId + "-" + key;
            }
            public String refund(String paymentId, BigDecimal amount, String currency) {
                return "refund-" + paymentId + "-" + currency;
            }
        };
    }

    @Bean
    ExternalHttpClient externalHttpClient() {
        return new ExternalHttpClient() {
            public String get(URI uri, Duration timeout) { return "GET " + uri; }
            public String post(URI uri, Map<String, Object> body, Duration timeout, String requestId) {
                return "POST " + uri + " " + requestId;
            }
        };
    }

    @Bean
    FileStore fileStore() {
        return new FileStore() {
            public Path exportRoot(String tenantId) {
                return Path.of(System.getProperty("java.io.tmpdir"), "tradeflow", tenantId);
            }
            public InputStream open(Path path) {
                return new ByteArrayInputStream(path.toString().getBytes());
            }
            public void write(Path path, InputStream content) {
                throw new UnsupportedOperationException("local benchmark adapter is read-only");
            }
        };
    }

    @Bean
    OutboxRepository outboxRepository() {
        return new OutboxRepository() {
            private final Map<String, OutboxEvent> events = new ConcurrentHashMap<>();
            private final Set<String> claimed = ConcurrentHashMap.newKeySet();
            public List<OutboxEvent> findReady(Instant now, int limit) {
                return events.values().stream()
                        .filter(event -> !event.availableAt().isAfter(now))
                        .limit(limit).toList();
            }
            public void save(OutboxEvent event) { events.put(event.id(), event); }
            public boolean claim(String eventId, String workerId) { return claimed.add(eventId); }
        };
    }

    @Bean
    UserRepository userRepository() {
        return new UserRepository() {
            private final Map<String, UserAccount> users = new ConcurrentHashMap<>();
            public Optional<UserAccount> findById(String id) { return Optional.ofNullable(users.get(id)); }
            public Optional<UserAccount> findByTenantAndId(String tenantId, String id) {
                return findById(id).filter(user -> user.tenantId().equals(tenantId));
            }
            public void save(UserAccount account) { users.put(account.id(), account); }
        };
    }

    @Bean
    AuditSink auditSink() {
        return (tenantId, action, detail) -> { };
    }

    @Bean
    TenantContext tenantContext() {
        return new TenantContext() {
            public String tenantId() { return "demo-tenant"; }
            public String userId() { return "demo-user"; }
            public boolean hasRole(String role) { return "ADMIN".equals(role); }
        };
    }
}

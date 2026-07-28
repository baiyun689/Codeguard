"""Materialize the controlled TradeFlow project-level review benchmark.

The generated snapshots are committed. This script is only the reproducible
source used to keep the clean baseline, twenty independent PR snapshots, diffs,
annotations, and isolated oracle tests in sync.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import cast

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "baseline" / "repo"
CASES = ROOT / "cases"
KNOWLEDGE_ROOT = (
    ROOT.parents[3] / "src" / "codeguard_agent" / "prompts" / "knowledge"
)


@dataclass(frozen=True)
class IssueSpec:
    slug: str
    title: str
    primary: str
    secondary: tuple[str, ...]
    reviewers: tuple[str, ...]
    coverage: str
    knowledge: tuple[str, ...]
    root_cause: str
    trigger: str
    consequence: str
    fix_action: str
    keywords: tuple[str, ...]
    severity: str = "WARNING"
    routing_hazard: bool = False
    taxonomy_gap: str | None = None


@dataclass(frozen=True)
class Scenario:
    number: int
    slug: str
    title: str
    class_name: str
    dimension: str
    capability: tuple[str, ...]
    method_names: tuple[str, str, str]
    methods: str
    issues: tuple[IssueSpec, IssueSpec, IssueSpec]

    @property
    def case_id(self) -> str:
        return f"pr-{self.number:02d}-{self.slug}"


def issue(
    slug: str,
    title: str,
    primary: str,
    *,
    secondary: tuple[str, ...] = (),
    reviewers: tuple[str, ...] = ("behavior",),
    coverage: str = "exact",
    root: str,
    trigger: str,
    consequence: str,
    fix: str,
    keywords: tuple[str, ...],
    severity: str = "WARNING",
    routing_hazard: bool = False,
    gap: str | None = None,
) -> IssueSpec:
    tags = (primary,) if coverage == "exact" else (primary, *secondary)
    domains = {
        "threat_model": "threat_model",
        "behavior": "behavior",
        "maintainability": "maintainability",
    }
    knowledge = tuple(
        f"{domains[reviewer]}/{tag}"
        for reviewer in reviewers
        for tag in tags
        if not tag.startswith("GAP:")
        and (KNOWLEDGE_ROOT / domains[reviewer] / f"{tag}.txt").is_file()
    )
    return IssueSpec(
        slug,
        title,
        primary,
        secondary,
        reviewers,
        coverage,
        knowledge or ("behavior/GENERAL_REVIEW",),
        root,
        trigger,
        consequence,
        fix,
        keywords,
        severity,
        routing_hazard,
        gap,
    )


def _child_pom(
    dependency: str | None = None,
    *,
    spring_web: bool = False,
    spring_context: bool = False,
) -> str:
    dependencies = []
    if dependency:
        dependencies.append(
            f"""
            <dependency>
              <groupId>com.codeguard.benchmark</groupId>
              <artifactId>{dependency}</artifactId>
              <version>${{project.version}}</version>
            </dependency>"""
        )
    if spring_web:
        dependencies.append(
            """
            <dependency>
              <groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter-web</artifactId>
            </dependency>"""
        )
    if spring_context or dependency == "tradeflow-domain":
        dependencies.append(
            """
            <dependency>
              <groupId>org.springframework</groupId>
              <artifactId>spring-context</artifactId>
            </dependency>
            <dependency>
              <groupId>org.springframework</groupId>
              <artifactId>spring-tx</artifactId>
            </dependency>"""
        )
    dependencies.append(
        """
            <dependency>
              <groupId>org.junit.jupiter</groupId>
              <artifactId>junit-jupiter</artifactId>
              <scope>test</scope>
            </dependency>"""
    )
    return f"""
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <modelVersion>4.0.0</modelVersion>
          <parent>
            <groupId>com.codeguard.benchmark</groupId>
            <artifactId>tradeflow-parent</artifactId>
            <version>1.0.0</version>
            <relativePath>../pom.xml</relativePath>
          </parent>
          <artifactId>MODULE_NAME</artifactId>
          <dependencies>{''.join(dependencies)}
          </dependencies>
        </project>
    """


def _boot_pom() -> str:
    return """
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <modelVersion>4.0.0</modelVersion>
          <parent>
            <groupId>com.codeguard.benchmark</groupId>
            <artifactId>tradeflow-parent</artifactId>
            <version>1.0.0</version>
            <relativePath>../pom.xml</relativePath>
          </parent>
          <artifactId>tradeflow-boot</artifactId>
          <dependencies>
            <dependency>
              <groupId>com.codeguard.benchmark</groupId>
              <artifactId>tradeflow-web</artifactId>
              <version>${project.version}</version>
            </dependency>
            <dependency>
              <groupId>com.codeguard.benchmark</groupId>
              <artifactId>tradeflow-persistence</artifactId>
              <version>${project.version}</version>
            </dependency>
            <dependency>
              <groupId>com.codeguard.benchmark</groupId>
              <artifactId>tradeflow-integrations</artifactId>
              <version>${project.version}</version>
            </dependency>
            <dependency>
              <groupId>com.codeguard.benchmark</groupId>
              <artifactId>tradeflow-worker</artifactId>
              <version>${project.version}</version>
            </dependency>
            <dependency>
              <groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter</artifactId>
            </dependency>
          </dependencies>
        </project>
    """


BASE_FILES = {
    "pom.xml": """
        <project xmlns="http://maven.apache.org/POM/4.0.0"
                 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
          <modelVersion>4.0.0</modelVersion>
          <groupId>com.codeguard.benchmark</groupId>
          <artifactId>tradeflow-parent</artifactId>
          <version>1.0.0</version>
          <packaging>pom</packaging>
          <properties>
            <maven.compiler.release>17</maven.compiler.release>
            <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
            <spring-boot.version>3.3.2</spring-boot.version>
            <junit.version>5.10.3</junit.version>
          </properties>
          <modules>
            <module>tradeflow-domain</module>
            <module>tradeflow-application</module>
            <module>tradeflow-web</module>
            <module>tradeflow-persistence</module>
            <module>tradeflow-integrations</module>
            <module>tradeflow-worker</module>
            <module>tradeflow-boot</module>
          </modules>
          <dependencyManagement>
            <dependencies>
              <dependency>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-dependencies</artifactId>
                <version>${spring-boot.version}</version>
                <type>pom</type>
                <scope>import</scope>
              </dependency>
              <dependency>
                <groupId>org.junit</groupId>
                <artifactId>junit-bom</artifactId>
                <version>${junit.version}</version>
                <type>pom</type>
                <scope>import</scope>
              </dependency>
            </dependencies>
          </dependencyManagement>
          <build>
            <pluginManagement>
              <plugins>
                <plugin>
                  <groupId>org.apache.maven.plugins</groupId>
                  <artifactId>maven-compiler-plugin</artifactId>
                  <version>3.11.0</version>
                </plugin>
                <plugin>
                  <groupId>org.apache.maven.plugins</groupId>
                  <artifactId>maven-surefire-plugin</artifactId>
                  <version>3.2.5</version>
                </plugin>
              </plugins>
            </pluginManagement>
          </build>
        </project>
    """,
    "README.md": """
        # TradeFlow

        TradeFlow is a Java 17/Spring Boot reference commerce platform used by
        the Codeguard controlled project-level review benchmark. The clean
        baseline models tenant-aware orders, inventory, payment, caching,
        outbound integrations and event delivery across seven Maven modules.
    """,
    "tradeflow-domain/pom.xml": _child_pom(),
    "tradeflow-application/pom.xml": _child_pom("tradeflow-domain"),
    "tradeflow-web/pom.xml": _child_pom("tradeflow-application", spring_web=True),
    "tradeflow-persistence/pom.xml": _child_pom("tradeflow-application", spring_context=True),
    "tradeflow-integrations/pom.xml": _child_pom("tradeflow-application", spring_context=True),
    "tradeflow-worker/pom.xml": _child_pom("tradeflow-application", spring_context=True),
    "tradeflow-boot/pom.xml": _boot_pom(),
    "tradeflow-domain/src/main/java/com/tradeflow/domain/Order.java": """
        package com.tradeflow.domain;

        import java.math.BigDecimal;
        import java.util.Objects;

        public final class Order {
            private final String id;
            private final String tenantId;
            private BigDecimal total;
            private BigDecimal refundable;
            private String status;
            private long version;

            public Order(String id, String tenantId, BigDecimal total, String status) {
                this.id = Objects.requireNonNull(id);
                this.tenantId = Objects.requireNonNull(tenantId);
                this.total = Objects.requireNonNull(total);
                this.refundable = total;
                this.status = Objects.requireNonNull(status);
            }

            public String id() { return id; }
            public String tenantId() { return tenantId; }
            public BigDecimal total() { return total; }
            public BigDecimal refundable() { return refundable; }
            public String status() { return status; }
            public long version() { return version; }
            public void total(BigDecimal value) { total = value; }
            public void refundable(BigDecimal value) { refundable = value; }
            public void status(String value) { status = value; }
            public void version(long value) { version = value; }
        }
    """,
    "tradeflow-domain/src/main/java/com/tradeflow/domain/InventoryItem.java": """
        package com.tradeflow.domain;

        public final class InventoryItem {
            private final String sku;
            private int available;
            private long version;

            public InventoryItem(String sku, int available) {
                this.sku = sku;
                this.available = available;
            }

            public String sku() { return sku; }
            public int available() { return available; }
            public long version() { return version; }
            public void available(int value) { available = value; }
            public void version(long value) { version = value; }
        }
    """,
    "tradeflow-domain/src/main/java/com/tradeflow/domain/OutboxEvent.java": """
        package com.tradeflow.domain;

        import java.time.Instant;

        public record OutboxEvent(
                String id, String aggregateId, long version, String payload,
                String state, int attempts, Instant availableAt) {
            public OutboxEvent sent() {
                return new OutboxEvent(id, aggregateId, version, payload, "SENT", attempts, availableAt);
            }
            public OutboxEvent retryAt(Instant next) {
                return new OutboxEvent(id, aggregateId, version, payload, "PENDING", attempts + 1, next);
            }
        }
    """,
    "tradeflow-domain/src/main/java/com/tradeflow/domain/UserAccount.java": """
        package com.tradeflow.domain;

        import java.util.Set;

        public record UserAccount(String id, String tenantId, Set<String> roles, long version) {
            public boolean hasRole(String role) { return roles.contains(role); }
        }
    """,
    "tradeflow-domain/src/test/java/com/tradeflow/domain/OrderTest.java": """
        package com.tradeflow.domain;

        import static org.junit.jupiter.api.Assertions.assertEquals;
        import java.math.BigDecimal;
        import org.junit.jupiter.api.Test;

        final class OrderTest {
            @Test
            void status_change_preserves_tenant_and_amount() {
                Order order = new Order(
                        "order-1", "tenant-a", new BigDecimal("19.90"), "CREATED");

                order.status("PAID");

                assertEquals("tenant-a", order.tenantId());
                assertEquals(new BigDecimal("19.90"), order.total());
                assertEquals("PAID", order.status());
            }
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/OrderRepository.java": """
        package com.tradeflow.application.port;

        import com.tradeflow.domain.Order;
        import java.util.List;
        import java.util.Optional;

        public interface OrderRepository {
            Optional<Order> findById(String id);
            Optional<Order> findByTenantAndId(String tenantId, String id);
            List<Order> search(String tenantId, String expression, int offset, int limit);
            void save(Order order);
            boolean saveIfVersion(Order order, long expectedVersion);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/InventoryRepository.java": """
        package com.tradeflow.application.port;

        import com.tradeflow.domain.InventoryItem;
        import java.util.Optional;

        public interface InventoryRepository {
            Optional<InventoryItem> findBySku(String sku);
            void save(InventoryItem item);
            boolean reserveIfAvailable(String sku, int quantity, long version);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/PaymentGateway.java": """
        package com.tradeflow.application.port;

        import java.math.BigDecimal;

        public interface PaymentGateway {
            String charge(String tenantId, String orderId, BigDecimal amount, String idempotencyKey);
            String refund(String paymentId, BigDecimal amount, String currency);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/EventPublisher.java": """
        package com.tradeflow.application.port;

        public interface EventPublisher {
            void publish(String topic, String key, String payload);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/CacheStore.java": """
        package com.tradeflow.application.port;

        import java.time.Duration;
        import java.util.Optional;

        public interface CacheStore {
            Optional<String> get(String key);
            void put(String key, String value, Duration ttl);
            void evict(String key);
            long increment(String key, Duration ttl);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/ExternalHttpClient.java": """
        package com.tradeflow.application.port;

        import java.net.URI;
        import java.time.Duration;
        import java.util.Map;

        public interface ExternalHttpClient {
            String get(URI uri, Duration timeout);
            String post(URI uri, Map<String, Object> body, Duration timeout, String requestId);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/FileStore.java": """
        package com.tradeflow.application.port;

        import java.io.InputStream;
        import java.nio.file.Path;

        public interface FileStore {
            Path exportRoot(String tenantId);
            InputStream open(Path path);
            void write(Path path, InputStream content);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/OutboxRepository.java": """
        package com.tradeflow.application.port;

        import com.tradeflow.domain.OutboxEvent;
        import java.time.Instant;
        import java.util.List;

        public interface OutboxRepository {
            List<OutboxEvent> findReady(Instant now, int limit);
            void save(OutboxEvent event);
            boolean claim(String eventId, String workerId);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/UserRepository.java": """
        package com.tradeflow.application.port;

        import com.tradeflow.domain.UserAccount;
        import java.util.Optional;

        public interface UserRepository {
            Optional<UserAccount> findById(String id);
            Optional<UserAccount> findByTenantAndId(String tenantId, String id);
            void save(UserAccount account);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/port/AuditSink.java": """
        package com.tradeflow.application.port;

        public interface AuditSink {
            void record(String tenantId, String action, String detail);
        }
    """,
    "tradeflow-application/src/main/java/com/tradeflow/application/security/TenantContext.java": """
        package com.tradeflow.application.security;

        public interface TenantContext {
            String tenantId();
            String userId();
            boolean hasRole(String role);
        }
    """,
    "tradeflow-persistence/src/main/java/com/tradeflow/persistence/InMemoryOrderRepository.java": """
        package com.tradeflow.persistence;

        import com.tradeflow.application.port.OrderRepository;
        import com.tradeflow.domain.Order;
        import java.util.List;
        import java.util.Map;
        import java.util.Optional;
        import java.util.concurrent.ConcurrentHashMap;
        import org.springframework.stereotype.Repository;

        @Repository
        public final class InMemoryOrderRepository implements OrderRepository {
            private final Map<String, Order> orders = new ConcurrentHashMap<>();

            public Optional<Order> findById(String id) { return Optional.ofNullable(orders.get(id)); }
            public Optional<Order> findByTenantAndId(String tenantId, String id) {
                return findById(id).filter(order -> order.tenantId().equals(tenantId));
            }
            public List<Order> search(String tenantId, String expression, int offset, int limit) {
                return orders.values().stream().filter(o -> o.tenantId().equals(tenantId))
                        .skip(offset).limit(limit).toList();
            }
            public void save(Order order) { orders.put(order.id(), order); }
            public boolean saveIfVersion(Order order, long expectedVersion) {
                return orders.computeIfPresent(order.id(), (id, current) ->
                        current.version() == expectedVersion ? order : current) == order;
            }
        }
    """,
    "tradeflow-persistence/src/main/java/com/tradeflow/persistence/InMemoryInventoryRepository.java": """
        package com.tradeflow.persistence;

        import com.tradeflow.application.port.InventoryRepository;
        import com.tradeflow.domain.InventoryItem;
        import java.util.Map;
        import java.util.Optional;
        import java.util.concurrent.ConcurrentHashMap;
        import org.springframework.stereotype.Repository;

        @Repository
        public final class InMemoryInventoryRepository implements InventoryRepository {
            private final Map<String, InventoryItem> items = new ConcurrentHashMap<>();
            public Optional<InventoryItem> findBySku(String sku) { return Optional.ofNullable(items.get(sku)); }
            public void save(InventoryItem item) { items.put(item.sku(), item); }
            public synchronized boolean reserveIfAvailable(String sku, int quantity, long version) {
                InventoryItem item = items.get(sku);
                if (item == null || item.version() != version || item.available() < quantity) {
                    return false;
                }
                item.available(item.available() - quantity);
                item.version(version + 1);
                return true;
            }
        }
    """,
    "tradeflow-persistence/src/test/java/com/tradeflow/persistence/InMemoryInventoryRepositoryTest.java": """
        package com.tradeflow.persistence;

        import static org.junit.jupiter.api.Assertions.assertEquals;
        import static org.junit.jupiter.api.Assertions.assertFalse;
        import static org.junit.jupiter.api.Assertions.assertTrue;
        import com.tradeflow.domain.InventoryItem;
        import org.junit.jupiter.api.Test;

        final class InMemoryInventoryRepositoryTest {
            @Test
            void reservation_checks_quantity_and_version_atomically() {
                InMemoryInventoryRepository repository =
                        new InMemoryInventoryRepository();
                repository.save(new InventoryItem("sku-1", 2));

                assertTrue(repository.reserveIfAvailable("sku-1", 2, 0));
                assertFalse(repository.reserveIfAvailable("sku-1", 1, 0));
                assertEquals(
                        0,
                        repository.findBySku("sku-1").orElseThrow().available());
            }
        }
    """,
    "tradeflow-integrations/src/main/java/com/tradeflow/integrations/RecordingEventPublisher.java": """
        package com.tradeflow.integrations;

        import com.tradeflow.application.port.EventPublisher;
        import java.util.List;
        import java.util.concurrent.CopyOnWriteArrayList;
        import org.springframework.stereotype.Component;

        @Component
        public final class RecordingEventPublisher implements EventPublisher {
            private final List<String> events = new CopyOnWriteArrayList<>();
            public void publish(String topic, String key, String payload) {
                events.add(topic + ":" + key + ":" + payload);
            }
            public List<String> events() { return List.copyOf(events); }
        }
    """,
    "tradeflow-web/src/main/java/com/tradeflow/web/ApiErrorHandler.java": """
        package com.tradeflow.web;

        import org.springframework.http.HttpStatus;
        import org.springframework.http.ResponseEntity;
        import org.springframework.web.bind.annotation.ExceptionHandler;
        import org.springframework.web.bind.annotation.RestControllerAdvice;

        @RestControllerAdvice
        public final class ApiErrorHandler {
            @ExceptionHandler(IllegalArgumentException.class)
            ResponseEntity<String> invalid(IllegalArgumentException error) {
                return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(error.getMessage());
            }
        }
    """,
    "tradeflow-worker/src/main/java/com/tradeflow/worker/WorkerMarker.java": """
        package com.tradeflow.worker;

        public interface WorkerMarker {
            String workerName();
        }
    """,
    "tradeflow-boot/src/main/java/com/tradeflow/TradeFlowApplication.java": """
        package com.tradeflow;

        import org.springframework.boot.SpringApplication;
        import org.springframework.boot.autoconfigure.SpringBootApplication;

        @SpringBootApplication
        public class TradeFlowApplication {
            public static void main(String[] args) {
                SpringApplication.run(TradeFlowApplication.class, args);
            }
        }
    """,
    "tradeflow-boot/src/main/java/com/tradeflow/LocalAdaptersConfiguration.java": """
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
    """,
    "tradeflow-boot/src/main/resources/application.yaml": """
        spring:
          application:
            name: tradeflow
        server:
          port: 8080
    """,
}


def _scenario(
    number: int,
    slug: str,
    title: str,
    class_name: str,
    methods: str,
    issues: tuple[IssueSpec, IssueSpec, IssueSpec],
    *,
    dimension: str = "logic",
    capability: tuple[str, ...] = ("call-path", "state-impact"),
) -> Scenario:
    method_names = cast(
        tuple[str, str, str],
        tuple(item.slug for item in issues),
    )
    return Scenario(
        number,
        slug,
        title,
        class_name,
        dimension,
        capability,
        method_names,
        _normalise_method_source(methods),
        issues,
    )


def _normalise_method_source(methods: str) -> str:
    lines = dedent(methods).strip().splitlines()
    positive_indents = [
        len(line) - len(line.lstrip())
        for line in lines
        if line.strip() and line[:1].isspace()
    ]
    shift = max(0, min(positive_indents, default=4) - 4)
    if not shift:
        return "\n".join(lines)
    return "\n".join(
        line[shift:] if line[:shift].isspace() else line
        for line in lines
    )


SCENARIOS = [
    _scenario(
        1,
        "tenant-order-update",
        "多租户订单更新",
        "TenantOrderUpdate",
        """
        public Object tenantLookup(Map<String, String> request) {
            Order order = orders.findById(request.get("orderId")).orElseThrow();
            order.status(request.get("status"));
            orders.save(order);
            return order;
        }

        public Object mutableProjection(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            order.total(new BigDecimal(request.get("total")));
            order.status(request.get("status"));
            orders.save(order);
            return order;
        }

        public Object prematureAudit(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            audit.record(context.tenantId(), "ORDER_UPDATED", order.id());
            order.status(request.get("status"));
            orders.save(order);
            return order;
        }
        """,
        (
            issue("tenantLookup", "订单更新遗漏租户条件", "AUTHORIZATION", secondary=("SQL_DATA_ACCESS",), reviewers=("threat_model", "behavior"), coverage="composite", root="按全局订单 ID 查询而未绑定当前租户", trigger="另一租户用户提交可猜测的订单 ID", consequence="跨租户订单状态被修改", fix="使用 findByTenantAndId 并以可信 TenantContext 约束查询", keywords=("越权", "tenant", "authorization"), severity="CRITICAL", routing_hazard=True),
            issue("mutableProjection", "更新 DTO 可覆盖服务端字段", "INPUT_VALIDATION", secondary=("API_CONTRACT",), reviewers=("threat_model", "behavior"), coverage="composite", root="通用更新请求直接映射金额和状态", trigger="调用方提交自行选择的 total 或内部状态", consequence="订单金额或状态机被绕过", fix="使用按动作定义的命令并在领域层计算金额和状态", keywords=("mass assignment", "金额", "状态"), severity="CRITICAL"),
            issue("prematureAudit", "提交前发布审计副作用", "TRANSACTION_ATOMICITY", secondary=("MESSAGE_DELIVERY",), reviewers=("behavior",), coverage="composite", root="外部审计副作用发生在持久化成功之前", trigger="保存订单失败或事务随后回滚", consequence="审计系统记录不存在的成功操作", fix="提交后通过 outbox 发布审计事件", keywords=("事务", "审计", "提交"), routing_hazard=True),
        ),
        dimension="security",
    ),
    _scenario(
        2,
        "checkout",
        "Checkout 编排",
        "Checkout",
        """
        public Object uncompensatedCharge(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            String paymentId = payments.charge(context.tenantId(), order.id(), order.total(), request.get("requestId"));
            InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
            if (!inventory.reserveIfAvailable(item.sku(), Integer.parseInt(request.get("quantity")), item.version())) {
                throw new IllegalStateException("inventory unavailable after payment " + paymentId);
            }
            return paymentId;
        }

        public Object localTransaction(Map<String, String> request) {
            return persistCheckout(request);
        }

        @Transactional
        public Object persistCheckout(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            order.status("PAID");
            orders.save(order);
            return order;
        }

        public Object sharedIdempotency(Map<String, String> request) {
            String key = "checkout:" + request.get("requestId");
            return cache.get(key).orElseGet(() -> {
                String result = payments.charge(context.tenantId(), request.get("orderId"),
                        new BigDecimal(request.get("amount")), request.get("requestId"));
                cache.put(key, result, Duration.ofHours(24));
                return result;
            });
        }
        """,
        (
            issue("uncompensatedCharge", "扣款后库存失败无补偿", "TRANSACTION_ATOMICITY", root="不可逆支付先于库存预留且失败路径没有退款补偿", trigger="支付成功后库存版本冲突或库存不足", consequence="用户被扣款但订单无法履约", fix="先可靠预留或实现持久化 Saga/补偿退款", keywords=("扣款", "库存", "补偿"), severity="CRITICAL"),
            issue("localTransaction", "事务方法通过同类调用绕过代理", "TRANSACTION_ATOMICITY", root="事务方法由同一实例直接调用", trigger="后续持久化步骤抛出运行时异常", consequence="预期原子更新可能部分提交", fix="将事务 seam 移到独立 Spring bean 的公开入口", keywords=("Transactional", "self invocation", "事务")),
            issue("sharedIdempotency", "幂等键未包含租户", "IDEMPOTENCY_RETRY", root="全局缓存键只使用客户端 requestId", trigger="两个租户使用相同 requestId", consequence="一个租户收到另一租户的支付结果或请求被错误抑制", fix="以 tenantId、业务动作和 requestId 共同组成唯一键", keywords=("幂等", "tenant", "requestId"), severity="CRITICAL"),
        ),
    ),
]


def _more_scenarios() -> list[Scenario]:
    """Keep the scenario catalogue readable by building the remaining entries."""
    rows = [
        (3, "coupon-pricing", "优惠券与定价", "CouponPricing", "BUSINESS_INVARIANT", "BUSINESS_INVARIANT", "CACHE_CONSISTENCY"),
        (4, "inventory-reservation", "库存预留", "InventoryReservation", "CONCURRENCY_CONSISTENCY", "CONCURRENCY_CONSISTENCY", "IDEMPOTENCY_RETRY"),
        (5, "payment-webhook", "支付 Webhook", "PaymentWebhook", "INPUT_VALIDATION", "IDEMPOTENCY_RETRY", "TRANSACTION_ATOMICITY"),
        (6, "file-export", "文件导出", "FileExport", "FILE_PATH_IO", "AUTHORIZATION", "CONCURRENCY_CONSISTENCY"),
        (7, "jwt-key-rotation", "JWT 与密钥轮换", "JwtKeyRotation", "SSRF_OUTBOUND", "TEMPORAL_SEMANTICS", "CACHE_CONSISTENCY"),
        (8, "order-events", "订单事件消费", "OrderEvents", "MESSAGE_DELIVERY", "ERROR_HANDLING", "CONCURRENCY_CONSISTENCY"),
        (9, "outbox-worker", "Outbox Worker", "OutboxDelivery", "CONCURRENCY_CONSISTENCY", "MESSAGE_DELIVERY", "TEMPORAL_SEMANTICS"),
        (10, "refund-flow", "退款流程", "RefundFlow", "BUSINESS_INVARIANT", "CONCURRENCY_CONSISTENCY", "NUMERIC_MONEY"),
        (11, "user-invitation", "用户邀请", "UserInvitation", "AUTHORIZATION", "CONCURRENCY_CONSISTENCY", "TRANSACTION_ATOMICITY"),
        (12, "order-search", "订单搜索", "OrderSearch", "INJECTION", "PERFORMANCE", "AUTHORIZATION"),
        (13, "bulk-import", "批量商品导入", "BulkImport", "FILE_PATH_IO", "ERROR_HANDLING", "INJECTION"),
        (14, "product-cache", "商品缓存刷新", "ProductCache", "CACHE_CONSISTENCY", "TRANSACTION_ATOMICITY", "CACHE_CONSISTENCY"),
        (15, "login-rate-limit", "登录限流", "LoginRateLimit", "AUTHENTICATION_SESSION", "CONCURRENCY_CONSISTENCY", "ERROR_HANDLING"),
        (16, "notification-template", "通知模板", "NotificationTemplate", "INJECTION", "AUTHORIZATION", "IDEMPOTENCY_RETRY"),
        (17, "shipment-integration", "物流创建", "ShipmentIntegration", "SSRF_OUTBOUND", "IDEMPOTENCY_RETRY", "PERFORMANCE"),
        (18, "admin-report", "管理员报表", "AdminReport", "AUTHORIZATION", "PERFORMANCE", "INJECTION"),
        (19, "order-state-machine", "订单状态机", "OrderStateMachine", "BUSINESS_INVARIANT", "CONCURRENCY_CONSISTENCY", "MESSAGE_DELIVERY"),
        (20, "tenant-config", "动态租户配置", "TenantConfig", "CONFIG_SECURITY", "CONCURRENCY_CONSISTENCY", "TEMPORAL_SEMANTICS"),
    ]
    return [_generated_scenario(*row) for row in rows]


def _generated_scenario(
    number: int,
    slug: str,
    title: str,
    class_name: str,
    tag_one: str,
    tag_two: str,
    tag_three: str,
) -> Scenario:
    specs = _scenario_issue_specs(number, tag_one, tag_two, tag_three)
    methods = "\n\n".join(
        _method_for(number, index, spec.slug, spec.primary)
        for index, spec in enumerate(specs, 1)
    )
    dimension = "security" if any(
        reviewer == "threat_model" for spec in specs for reviewer in spec.reviewers
    ) else "logic"
    return _scenario(number, slug, title, class_name, methods, specs, dimension=dimension)


def _scenario_issue_specs(
    number: int, tag_one: str, tag_two: str, tag_three: str
) -> tuple[IssueSpec, IssueSpec, IssueSpec]:
    descriptions = ISSUE_DESCRIPTIONS[number]
    tags = (tag_one, tag_two, tag_three)
    result = []
    for index, (tag, description) in enumerate(zip(tags, descriptions, strict=True), 1):
        gap = tag if tag in {"BUSINESS_INVARIANT", "NUMERIC_MONEY", "TEMPORAL_SEMANTICS"} else None
        primary = description.get("primary", _compatible_tag(tag))
        secondary = tuple(description.get("secondary", ()))
        coverage = description.get(
            "coverage", "gap" if gap else ("composite" if secondary else "exact")
        )
        reviewers = tuple(description.get("reviewers", _reviewers_for(primary, secondary)))
        result.append(issue(
            METHOD_NAMES[number][index - 1],
            description["title"],
            primary,
            secondary=secondary,
            reviewers=reviewers,
            coverage=coverage,
            root=description["root"],
            trigger=description["trigger"],
            consequence=description["consequence"],
            fix=description["fix"],
            keywords=tuple(description["keywords"]),
            severity=description.get("severity", "WARNING"),
            routing_hazard=description.get("routing_hazard", index == 2 and number % 2 == 0),
            gap=gap,
        ))
    return tuple(result)  # type: ignore[return-value]


def _compatible_tag(tag: str) -> str:
    return {
        "BUSINESS_INVARIANT": "API_CONTRACT",
        "NUMERIC_MONEY": "API_CONTRACT",
        "TEMPORAL_SEMANTICS": "IDEMPOTENCY_RETRY",
    }.get(tag, tag)


TAG_REVIEWERS: dict[str, tuple[str, ...]] = {
    "AUTHORIZATION": ("threat_model", "behavior"),
    "AUTHENTICATION_SESSION": ("threat_model", "behavior"),
    "WEB_SECURITY_CONFIG": ("threat_model",),
    "INPUT_VALIDATION": ("threat_model", "behavior"),
    "INJECTION": ("threat_model", "behavior"),
    "SQL_DATA_ACCESS": ("behavior",),
    "FILE_PATH_IO": ("threat_model", "behavior"),
    "SSRF_OUTBOUND": ("threat_model", "behavior"),
    "CONFIG_SECURITY": ("threat_model",),
    "DATA_EXPOSURE": ("threat_model", "behavior"),
    "DESERIALIZATION": ("threat_model",),
    "TRANSACTION_ATOMICITY": ("behavior",),
    "CONCURRENCY_CONSISTENCY": ("behavior",),
    "IDEMPOTENCY_RETRY": ("behavior",),
    "CACHE_CONSISTENCY": ("behavior",),
    "MESSAGE_DELIVERY": ("behavior",),
    "ERROR_HANDLING": ("behavior",),
    "NULL_STATE_SAFETY": ("behavior",),
    "RESOURCE_LIFECYCLE": ("behavior", "maintainability"),
    "API_CONTRACT": ("behavior", "maintainability"),
    "PERFORMANCE": ("behavior", "maintainability"),
    "COMPLEXITY_CONTROL_FLOW": ("maintainability",),
    "DUPLICATION_DESIGN": ("maintainability",),
    "OBSERVABILITY_TESTABILITY": ("maintainability",),
}


def _reviewers_for(primary: str, secondary: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        reviewer
        for tag in (primary, *secondary)
        for reviewer in TAG_REVIEWERS.get(tag, ("behavior",))
    ))


METHOD_NAMES: dict[int, tuple[str, str, str]] = {
    3: ("calculateCombinedDiscount", "applyCouponRules", "loadCustomerPrice"),
    4: ("reserveStock", "reserveWithLocalLock", "releaseExpiredReservation"),
    5: ("verifyWebhookSignature", "acceptWebhookEvent", "applyPaymentEvent"),
    6: ("openExport", "openOwnedExport", "openVerifiedExport"),
    7: ("loadSigningKey", "isTokenActive", "loadTenantRoles"),
    8: ("changeOrderStatus", "consumeOrderEvent", "applyVersionedEvent"),
    9: ("deliverReadyEvents", "deliverOneEvent", "scheduleRetry"),
    10: ("refundAgainstOrder", "refundRemainingAmount", "refundConvertedAmount"),
    11: ("inviteTenantMember", "inviteWithRoleCheck", "sendInvitation"),
    12: ("searchWithSort", "searchPage", "searchWithFallback"),
    13: ("resolveArchiveEntry", "importRows", "renderImportError"),
    14: ("loadProduct", "updateProduct", "createProduct"),
    15: ("countByClientAddress", "recordLoginFailure", "checkRateLimit"),
    16: ("renderNotification", "unsubscribeRecipient", "sendNotification"),
    17: ("createShipment", "retryShipment", "createShipmentWithTimeout"),
    18: ("runScheduledExport", "renderFullReport", "renderReportRow"),
    19: ("transitionOrder", "mapOrderUpdate", "publishCompensation"),
    20: ("loadTenantSecret", "reloadConfiguration", "publishConfiguration"),
}


ISSUE_DESCRIPTIONS: dict[int, tuple[dict, dict, dict]] = {
    3: (
        dict(title="折扣计算顺序破坏价格规则", root="百分比折扣在固定抵扣前应用，与领域规定顺序相反", trigger="订单同时使用满减券和会员折扣", consequence="应付金额系统性偏高或偏低", fix="把折扣顺序封装为领域策略并用明确阶段执行", keywords=("折扣", "顺序", "金额")),
        dict(title="同一优惠券经两条规则重复应用", root="优惠券 ID 未在规则组合中去重", trigger="券同时匹配会员与活动规则", consequence="同一优惠被扣减两次", fix="按券实例建立唯一应用记录并在原子边界内消费", keywords=("优惠券", "重复", "discount")),
        dict(title="价格缓存键遗漏客户等级", root="缓存键只含商品和租户，不含影响价格的客户等级", trigger="不同等级客户依次查询同一商品", consequence="后一个客户读取前一个等级价格", fix="把所有定价维度纳入 key 或缓存基础价格", keywords=("缓存", "价格", "等级")),
    ),
    4: (
        dict(title="库存读改写导致超卖", root="校验与保存之间没有版本条件或数据库原子更新", trigger="两个请求同时预留最后一批库存", consequence="可用库存变为负数并产生无法履约订单", fix="使用 reserveIfAvailable 条件更新并检查影响行数", keywords=("超卖", "并发", "库存"), severity="CRITICAL"),
        dict(title="按请求创建的锁无法互斥", root="每次调用都创建新的锁对象", trigger="同一 SKU 被多个线程同时预留", consequence="临界区实际并未串行化", fix="使用数据库锁、按 SKU 共享锁或原子条件更新", keywords=("锁", "并发", "SKU")),
        dict(title="超时释放任务可重复归还库存", root="释放操作未记录 reservation 的完成状态", trigger="调度器重试同一超时预留", consequence="库存被多次增加", fix="用 reservationId 和状态条件实现幂等释放", keywords=("幂等", "释放", "库存")),
    ),
    5: (
        dict(title="使用解析后 JSON 验证 Webhook 签名", root="签名输入不是支付方发送的原始字节", trigger="JSON 空白、字段顺序或数字格式被解析器规范化", consequence="合法回调被拒绝或错误实现下可绕过认证", fix="在反序列化前保留原始 body 并做恒时签名比较", keywords=("webhook", "签名", "原始"), secondary=("AUTHENTICATION_SESSION",), coverage="exact", reviewers=("threat_model", "behavior")),
        dict(title="Webhook 缺少重放窗口", root="事件去重没有绑定签名时间戳和有效窗口", trigger="攻击者重放曾经合法的支付事件", consequence="订单状态或退款副作用重复执行", fix="校验时间戳窗口并持久化事件 ID 去重", keywords=("重放", "webhook", "幂等")),
        dict(title="先标记回调完成再更新订单", root="处理标记和业务状态更新顺序错误且不原子", trigger="订单保存失败", consequence="重试被完成标记挡住，支付状态永久丢失", fix="在同一事务中更新状态和 inbox，提交后再确认", keywords=("事务", "processed", "订单"), secondary=("MESSAGE_DELIVERY",), coverage="exact"),
    ),
    6: (
        dict(title="导出路径可逃逸租户目录", root="resolve 后未 normalize 并验证根目录", trigger="文件名包含编码后的父目录片段", consequence="读取其他租户或服务器文件", fix="规范化并验证 startsWith 专用根目录", keywords=("路径穿越", "导出", "文件"), severity="CRITICAL"),
        dict(title="导出授权信任请求 owner", root="资源所有者来自调用方而非可信身份上下文", trigger="调用方把 owner 参数替换成目标用户", consequence="下载他人的报表", fix="从 TenantContext 获取主体并按资源归属查询", keywords=("越权", "owner", "导出"), severity="CRITICAL"),
        dict(title="检查与打开之间存在文件竞态", root="权限检查和实际打开使用两个独立路径解析步骤", trigger="检查后目标被符号链接或并发替换", consequence="打开与已授权文件不同的对象", fix="使用安全文件句柄或不可变对象存储键消除 TOCTOU", keywords=("TOCTOU", "并发", "文件"), secondary=("FILE_PATH_IO",), coverage="exact"),
    ),
    7: (
        dict(title="JWT kid 可控制远程密钥地址", root="未受信任 kid 被拼成任意 URI", trigger="攻击者提交自定义 kid", consequence="服务访问内网或云元数据地址", fix="kid 只能映射预配置 issuer 的本地 key set", keywords=("SSRF", "kid", "JWT"), severity="CRITICAL"),
        dict(title="JWT 过期时间单位比较错误", root="秒级 exp 与毫秒级当前时间直接比较", trigger="任意本应过期的 token", consequence="凭据有效期被错误延长或全部被拒绝", fix="使用 Instant 并明确 epoch 单位", keywords=("过期", "时间", "JWT")),
        dict(title="角色缓存键遗漏租户", root="角色缓存只按用户 ID 建键", trigger="相同用户标识存在于不同租户", consequence="低权限租户继承另一租户角色", fix="缓存键包含 issuer、tenant 和 subject", keywords=("缓存", "角色", "租户"), secondary=("AUTHORIZATION",), coverage="exact", reviewers=("threat_model", "behavior"), severity="CRITICAL"),
    ),
    8: (
        dict(title="订单事件在事务提交前发布", root="外部事件发布先于订单事务提交", trigger="发布成功后数据库提交失败", consequence="消费者观察到不存在的订单状态", fix="使用 transactional outbox 并在提交后投递", keywords=("事件", "事务", "提交"), secondary=("TRANSACTION_ATOMICITY",), coverage="exact"),
        dict(title="消费异常被吞后仍确认消息", root="宽泛 catch 将处理失败转换为正常返回", trigger="任意下游暂时失败", consequence="消息 offset 前进且事件永久丢失", fix="传播异常触发重试并配置 DLQ", keywords=("异常", "ack", "消息"), secondary=("MESSAGE_DELIVERY",), coverage="exact"),
        dict(title="旧版本事件覆盖新状态", root="消费者未比较事件聚合版本", trigger="网络重试导致事件乱序", consequence="订单回退到旧状态", fix="持久化最后版本并仅原子接受更大版本", keywords=("版本", "乱序", "事件"), secondary=("MESSAGE_DELIVERY",), coverage="exact"),
    ),
    9: (
        dict(title="多实例领取同一 Outbox 事件", root="查询 ready 事件后未 claim 或 skip locked", trigger="两个 worker 同时轮询", consequence="相同业务事件重复投递", fix="原子 claim、数据库锁或租约领取", keywords=("outbox", "并发", "重复"), secondary=("MESSAGE_DELIVERY",), coverage="exact"),
        dict(title="Broker 确认前标记 sent", root="本地状态先于 broker 确认变更", trigger="发送超时或 broker 拒绝", consequence="事件被永久标记完成但实际未送达", fix="确认成功后更新，或让未确认状态可安全重试", keywords=("sent", "确认", "消息"), secondary=("TRANSACTION_ATOMICITY",), coverage="exact"),
        dict(title="退避时间单位错误", root="秒级配置被当毫秒加入 Instant", trigger="消息持续失败进入重试", consequence="形成高频重试并压垮依赖", fix="使用 Duration 类型贯穿配置和计算", keywords=("退避", "时间", "重试")),
    ),
    10: (
        dict(title="退款额度使用原始金额校验", root="每次退款均与订单总额比较而非剩余可退额", trigger="一笔订单执行多次部分退款", consequence="累计退款超过实际支付金额", fix="在聚合内维护剩余额度并原子扣减", keywords=("退款", "额度", "金额"), severity="CRITICAL"),
        dict(title="并发退款突破剩余额度", root="读取剩余额度和保存退款之间无版本保护", trigger="两个退款请求同时通过余额检查", consequence="重复退款造成资金损失", fix="使用乐观锁或原子条件扣减并处理冲突", keywords=("退款", "并发", "版本"), severity="CRITICAL"),
        dict(title="币种换算发生两次舍入", root="汇率换算和网关金额转换分别舍入", trigger="非整数汇率和小数金额退款", consequence="账本与渠道金额产生不可对账差异", fix="内部保持高精度，仅在支付边界按币种舍入一次", keywords=("舍入", "币种", "金额")),
    ),
    11: (
        dict(title="邀请查询遗漏租户", root="按全局用户 ID 读取邀请对象", trigger="管理员提交另一租户用户 ID", consequence="跨租户创建或修改成员", fix="所有邀请查询绑定可信 tenantId", keywords=("邀请", "租户", "越权"), severity="CRITICAL"),
        dict(title="角色校验与邀请写入存在竞态", root="校验管理员版本后写入前未锁定或复验", trigger="管理员权限在并发请求中被撤销", consequence="已撤权主体仍能创建高权限邀请", fix="在同一原子边界校验角色版本并写入", keywords=("角色", "并发", "邀请"), secondary=("AUTHORIZATION",), coverage="composite"),
        dict(title="提交前发送外部邀请", root="邮件副作用发生在用户记录提交前", trigger="数据库保存失败", consequence="收件人得到无法兑换的幽灵邀请", fix="提交后 outbox 发送并支持幂等兑换", keywords=("事务", "邀请", "邮件")),
    ),
    12: (
        dict(title="排序字段进入原生查询", root="请求 sort 表达式未经枚举映射进入查询", trigger="调用方提交 SQL 片段作为排序字段", consequence="查询语义被篡改或数据泄露", fix="将公开排序键映射到固定列集合", keywords=("SQL注入", "排序", "查询"), secondary=("SQL_DATA_ACCESS",), coverage="composite", severity="CRITICAL"),
        dict(title="分页 offset 整数溢出", root="page 与 size 使用 int 乘法且校验发生在乘法前", trigger="提交极大页码", consequence="负 offset 绕过限制并触发大范围查询", fix="使用 long/Math.multiplyExact 并设置总上限", keywords=("分页", "溢出", "性能"), secondary=("INPUT_VALIDATION",), coverage="composite", reviewers=("threat_model", "maintainability")),
        dict(title="fallback 查询遗漏访问范围", root="主查询为空时改用不带租户条件的 fallback", trigger="当前租户没有匹配记录", consequence="返回其他租户订单", fix="所有查询分支共享强制租户 predicate", keywords=("fallback", "租户", "越权"), secondary=("SQL_DATA_ACCESS",), coverage="composite", severity="CRITICAL"),
    ),
    13: (
        dict(title="ZIP 条目逃逸导入目录", root="entry name 直接 resolve 到目标目录", trigger="上传包含 ../ 的压缩包", consequence="覆盖应用可写的任意文件", fix="normalize 后检查根目录并限制条目和总大小", keywords=("Zip Slip", "路径", "导入"), severity="CRITICAL"),
        dict(title="捕获行异常导致部分导入提交", root="行级异常被吞且外层事务继续提交", trigger="批次中间一行保存失败", consequence="调用方收到成功但数据仅部分导入", fix="按明确策略回滚整个批次或返回可恢复的逐行结果", keywords=("事务", "异常", "部分提交"), secondary=("TRANSACTION_ATOMICITY",), coverage="composite"),
        dict(title="错误报告存在 CSV 公式注入", root="外部字段以公式前缀原样写入 CSV", trigger="商品名以 =、+、- 或 @ 开头", consequence="运营人员打开文件时执行公式", fix="按 CSV 消费场景转义公式前缀", keywords=("CSV注入", "公式", "导出"), reviewers=("threat_model", "behavior"), severity="CRITICAL"),
    ),
    14: (
        dict(title="商品缓存键遗漏租户", root="缓存 key 只使用商品 ID", trigger="不同租户存在相同商品 ID", consequence="读取另一租户价格或配置", fix="key 加入 tenantId 并审计现有键空间", keywords=("缓存", "租户", "商品"), secondary=("AUTHORIZATION",), coverage="composite", severity="CRITICAL"),
        dict(title="事务提交前驱逐缓存", root="更新事务尚未成功就向其他实例广播失效", trigger="事务随后回滚且读请求重建缓存", consequence="缓存固化未提交状态或长期不一致", fix="提交后发布失效事件", keywords=("缓存", "事务", "失效"), secondary=("CACHE_CONSISTENCY",), coverage="composite"),
        dict(title="创建后未清除 negative cache", root="未找到结果被缓存，创建路径不驱逐该 key", trigger="先查询不存在商品再立即创建", consequence="有效商品在 TTL 内持续显示不存在", fix="创建提交后驱逐 negative cache", keywords=("negative cache", "创建", "缓存")),
    ),
    15: (
        dict(title="无条件信任转发来源地址", root="限流主体直接取首个 X-Forwarded-For", trigger="客户端伪造请求头", consequence="绕过登录失败限流", fix="只接受可信代理重写的规范化客户端地址", keywords=("X-Forwarded-For", "限流", "认证"), secondary=("INPUT_VALIDATION",), coverage="composite"),
        dict(title="Redis 计数 get/increment 非原子", root="阈值检查和增加是两个操作", trigger="并发登录失败请求", consequence="多个请求同时低于阈值并绕过锁定", fix="Lua/原子 increment 后判断返回值", keywords=("Redis", "并发", "限流")),
        dict(title="Redis 故障时登录限流 fail-open", root="缓存异常被捕获并直接允许认证继续", trigger="攻击者或故障导致 Redis 不可用", consequence="敏感登录入口失去暴力破解保护", fix="采用本地受限降级或对高风险入口 fail-closed", keywords=("fail-open", "异常", "登录"), secondary=("AUTHENTICATION_SESSION",), coverage="composite", severity="CRITICAL"),
    ),
    16: (
        dict(title="租户模板进入表达式求值", root="用户可控模板作为表达式源码解析", trigger="模板包含可执行表达式", consequence="读取服务数据或执行危险方法", fix="使用无表达式模板或严格变量白名单", keywords=("模板注入", "表达式", "通知"), severity="CRITICAL"),
        dict(title="退订查询遗漏租户", root="退订 token 解析后只按 subscriptionId 查询", trigger="构造或泄露另一个租户的订阅 ID", consequence="取消他人通知订阅", fix="token 绑定 tenant、recipient 和用途并按归属查询", keywords=("退订", "租户", "越权"), severity="CRITICAL"),
        dict(title="通知重试缺少业务幂等键", root="每次尝试生成新的外部 requestId", trigger="发送成功但响应丢失后重试", consequence="用户收到重复邮件或短信", fix="以 notificationId 作为稳定幂等键", keywords=("重试", "幂等", "通知")),
    ),
    17: (
        dict(title="物流回调地址形成 SSRF", root="请求 callbackUrl 直接交给出站客户端", trigger="用户控制 scheme 或 host", consequence="访问内网和云元数据", fix="固定合作方 base URL 并校验解析后的地址", keywords=("SSRF", "物流", "callback"), severity="CRITICAL"),
        dict(title="HTTP 重试重复创建运单", root="非幂等创建请求每次重试使用新 requestId", trigger="物流端已创建但响应超时", consequence="同一订单产生多个运单和费用", fix="以 orderId 生成稳定幂等键并查询已有结果", keywords=("重试", "运单", "幂等"), severity="CRITICAL"),
        dict(title="超时单位错误占满工作线程", root="秒级配置被构造成 Duration.ofMillis", trigger="物流端延迟或失联", consequence="请求线程长时间阻塞并级联耗尽", fix="用 Duration 配置并设置连接、响应与总预算", keywords=("超时", "线程", "性能"), secondary=("RESOURCE_LIFECYCLE",), coverage="composite", reviewers=("behavior", "maintainability")),
    ),
    18: (
        dict(title="异步报表路径绕过管理员鉴权", root="鉴权仅位于 Controller，worker 公开调用同一服务时无等价检查", trigger="低权限用户能投递导出任务", consequence="导出全租户敏感报表", fix="在应用用例 seam 强制授权", keywords=("报表", "鉴权", "异步"), severity="CRITICAL", routing_hazard=True),
        dict(title="报表全量加载导致内存放大", root="先 findAll 再在内存分页和序列化", trigger="大租户导出多年数据", consequence="堆内存耗尽并影响在线请求", fix="流式分页、背压和导出上限", keywords=("报表", "内存", "分页"), severity="CRITICAL"),
        dict(title="报表字段未防 CSV 注入", root="用户字段直接写入电子表格可执行单元格", trigger="订单备注以公式前缀开头", consequence="管理员打开报表时执行公式", fix="转义公式前缀并采用安全导出格式", keywords=("CSV注入", "报表", "公式"), severity="CRITICAL"),
    ),
    19: (
        dict(title="状态 ordinal 比较允许非法跳转", root="用枚举顺序代替显式状态转换图", trigger="从取消状态请求数值更大的状态", consequence="已终止订单重新进入履约", fix="在领域状态机显式列出允许边", keywords=("状态机", "非法跳转", "ordinal")),
        dict(title="映射过程丢失乐观锁版本", root="重建 Order 时 version 回到默认值", trigger="并发修改后提交映射结果", consequence="覆盖另一请求已提交的状态", fix="保留版本并使用 saveIfVersion 检查冲突", keywords=("版本", "映射", "并发"), secondary=("API_CONTRACT",), coverage="composite"),
        dict(title="补偿事件携带新版本而非被补偿版本", root="事件版本在状态递增后取值", trigger="下游按版本去重或排序", consequence="补偿被当成后续新事件并覆盖正确状态", fix="事件明确携带 causationVersion 和新 aggregateVersion", keywords=("补偿", "版本", "事件"), secondary=("CONCURRENCY_CONSISTENCY",), coverage="composite"),
    ),
    20: (
        dict(title="租户配置回退暴露全局密钥", root="租户缺失 secret 时返回全局生产 secret", trigger="新租户尚未完成配置", consequence="租户获得共享高权限凭据", fix="缺失安全配置时拒绝服务并使用租户隔离 secret", keywords=("配置", "密钥", "租户"), secondary=("DATA_EXPOSURE",), coverage="composite", severity="CRITICAL"),
        dict(title="共享配置 Map 并发修改", root="请求线程读取的可变 Map 被 reload 原地更新", trigger="热更新与请求读取交错", consequence="读取到混合版本或抛出并发异常", fix="构建不可变快照后用原子引用整体替换", keywords=("配置", "并发", "Map")),
        dict(title="配置未构造完成就发布引用", root="reload 在填充所有字段前替换可见引用", trigger="请求恰好落在热更新窗口", consequence="认证或支付读取到半初始化配置", fix="离线验证完整快照后一次原子发布", keywords=("配置", "可见性", "热更新")),
    ),
}


def _method_for(number: int, index: int, name: str, primary: str) -> str:
    template = METHOD_TEMPLATES[number][index - 1].replace(
        f"operation{index}", name, 1
    )
    lines = template.strip().splitlines()
    positive_indents = [
        len(line) - len(line.lstrip())
        for line in lines
        if line.strip() and line[:1].isspace()
    ]
    shift = min(positive_indents, default=0)
    return "\n".join(
        line[shift:] if shift and line[:shift].isspace() else line
        for line in lines
    )


METHOD_TEMPLATES: dict[int, tuple[str, str, str]] = {
    3: (
        """public Object operation1(Map<String, String> request) {
            BigDecimal subtotal = new BigDecimal(request.get("subtotal"));
            BigDecimal percent = new BigDecimal(request.get("percent"));
            BigDecimal fixed = new BigDecimal(request.get("fixed"));
            return subtotal.multiply(BigDecimal.ONE.subtract(percent)).subtract(fixed);
        }""",
        """public Object operation2(Map<String, String> request) {
            BigDecimal total = new BigDecimal(request.get("subtotal"));
            total = total.subtract(new BigDecimal(request.get("campaignCoupon")));
            return total.subtract(new BigDecimal(request.get("memberCoupon")));
        }""",
        """public Object operation3(Map<String, String> request) {
            String key = "price:" + context.tenantId() + ":" + request.get("productId");
            return cache.get(key).orElseGet(() -> {
                String price = request.get("calculatedPrice");
                cache.put(key, price, Duration.ofMinutes(15));
                return price;
            });
        }""",
    ),
    4: (
        """public Object operation1(Map<String, String> request) {
            InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
            int quantity = Integer.parseInt(request.get("quantity"));
            if (item.available() < quantity) throw new IllegalStateException("insufficient");
            item.available(item.available() - quantity);
            inventory.save(item);
            return item.available();
        }""",
        """public Object operation2(Map<String, String> request) {
            Object requestLock = new Object();
            synchronized (requestLock) {
                InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
                item.available(item.available() - Integer.parseInt(request.get("quantity")));
                inventory.save(item);
                return item.available();
            }
        }""",
        """public Object operation3(Map<String, String> request) {
            InventoryItem item = inventory.findBySku(request.get("sku")).orElseThrow();
            item.available(item.available() + Integer.parseInt(request.get("quantity")));
            inventory.save(item);
            events.publish("inventory.released", request.get("reservationId"), item.sku());
            return item.available();
        }""",
    ),
    5: (
        """public Object operation1(Map<String, String> request) {
            String normalized = request.entrySet().stream().sorted(Map.Entry.comparingByKey())
                    .map(entry -> entry.getKey() + "=" + entry.getValue()).collect(Collectors.joining("&"));
            return MessageDigest.isEqual(normalized.getBytes(StandardCharsets.UTF_8),
                    request.get("signature").getBytes(StandardCharsets.UTF_8));
        }""",
        """public Object operation2(Map<String, String> request) {
            String eventId = request.get("eventId");
            if (cache.get("webhook:" + eventId).isPresent()) return "duplicate";
            cache.put("webhook:" + eventId, "processed", Duration.ofDays(30));
            return "accepted";
        }""",
        """public Object operation3(Map<String, String> request) {
            cache.put("webhook:" + request.get("eventId"), "processed", Duration.ofDays(30));
            Order order = orders.findByTenantAndId(request.get("tenantId"), request.get("orderId")).orElseThrow();
            order.status("PAID");
            orders.save(order);
            return order;
        }""",
    ),
    6: (
        """public Object operation1(Map<String, String> request) {
            Path root = files.exportRoot(context.tenantId());
            Path target = root.resolve(URLDecoder.decode(request.get("file"), StandardCharsets.UTF_8));
            return files.open(target);
        }""",
        """public Object operation2(Map<String, String> request) {
            Path root = files.exportRoot(request.get("ownerTenant"));
            return files.open(root.resolve(request.get("file")));
        }""",
        """public Object operation3(Map<String, String> request) {
            Path target = files.exportRoot(context.tenantId()).resolve(request.get("file")).normalize();
            if (!Files.isRegularFile(target)) throw new IllegalArgumentException("missing");
            audit.record(context.tenantId(), "EXPORT", target.toString());
            return files.open(target);
        }""",
    ),
    7: (
        """public Object operation1(Map<String, String> request) {
            URI keyUri = URI.create(request.get("issuer") + "/keys/" + request.get("kid"));
            return http.get(keyUri, Duration.ofSeconds(2));
        }""",
        """public Object operation2(Map<String, String> request) {
            long expiresAtSeconds = Long.parseLong(request.get("exp"));
            return expiresAtSeconds > System.currentTimeMillis();
        }""",
        """public Object operation3(Map<String, String> request) {
            String key = "roles:" + request.get("subject");
            return cache.get(key).orElseGet(() -> {
                String roles = request.get("roles");
                cache.put(key, roles, Duration.ofMinutes(10));
                return roles;
            });
        }""",
    ),
    8: (
        """public Object operation1(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            events.publish("order.status", order.id(), request.get("status"));
            order.status(request.get("status"));
            orders.save(order);
            return order;
        }""",
        """public Object operation2(Map<String, String> request) {
            try {
                Order order = orders.findByTenantAndId(request.get("tenantId"), request.get("orderId")).orElseThrow();
                order.status(request.get("status"));
                orders.save(order);
            } catch (RuntimeException failure) {
                audit.record(request.get("tenantId"), "EVENT_FAILED", failure.getMessage());
            }
            return "ack";
        }""",
        """public Object operation3(Map<String, String> request) {
            Order order = orders.findByTenantAndId(request.get("tenantId"), request.get("orderId")).orElseThrow();
            order.status(request.get("status"));
            order.version(Long.parseLong(request.get("version")));
            orders.save(order);
            return order;
        }""",
    ),
    9: (
        """public Object operation1(Map<String, String> request) {
            List<OutboxEvent> ready = outbox.findReady(Instant.now(), 100);
            ready.forEach(event -> events.publish("outbox", event.aggregateId(), event.payload()));
            return ready.size();
        }""",
        """public Object operation2(Map<String, String> request) {
            OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();
            outbox.save(event.sent());
            events.publish("outbox", event.aggregateId(), event.payload());
            return event.id();
        }""",
        """public Object operation3(Map<String, String> request) {
            OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();
            long configuredSeconds = Long.parseLong(request.get("backoffSeconds"));
            outbox.save(event.retryAt(Instant.now().plusMillis(configuredSeconds)));
            return event.id();
        }""",
    ),
    10: (
        """public Object operation1(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            BigDecimal amount = new BigDecimal(request.get("amount"));
            if (amount.compareTo(order.total()) > 0) throw new IllegalArgumentException("too large");
            return payments.refund(request.get("paymentId"), amount, request.get("currency"));
        }""",
        """public Object operation2(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            BigDecimal amount = new BigDecimal(request.get("amount"));
            if (amount.compareTo(order.refundable()) > 0) throw new IllegalArgumentException("too large");
            order.refundable(order.refundable().subtract(amount));
            orders.save(order);
            return payments.refund(request.get("paymentId"), amount, request.get("currency"));
        }""",
        """public Object operation3(Map<String, String> request) {
            BigDecimal source = new BigDecimal(request.get("amount"));
            BigDecimal rate = new BigDecimal(request.get("rate"));
            BigDecimal ledger = source.multiply(rate).setScale(2, RoundingMode.HALF_UP);
            BigDecimal gateway = ledger.setScale(Integer.parseInt(request.get("minorUnits")), RoundingMode.HALF_UP);
            return payments.refund(request.get("paymentId"), gateway, request.get("currency"));
        }""",
    ),
    11: (
        """public Object operation1(Map<String, String> request) {
            UserAccount account = users.findById(request.get("userId")).orElseThrow();
            users.save(new UserAccount(account.id(), context.tenantId(), Set.of(request.get("role")), 0));
            return account.id();
        }""",
        """public Object operation2(Map<String, String> request) {
            UserAccount operator = users.findByTenantAndId(context.tenantId(), context.userId()).orElseThrow();
            if (!operator.hasRole("ADMIN")) throw new SecurityException("forbidden");
            UserAccount invited = new UserAccount(request.get("userId"), context.tenantId(), Set.of(request.get("role")), 0);
            users.save(invited);
            return invited;
        }""",
        """public Object operation3(Map<String, String> request) {
            events.publish("email.invitation", request.get("email"), request.get("token"));
            UserAccount invited = new UserAccount(request.get("userId"), context.tenantId(), Set.of("MEMBER"), 0);
            users.save(invited);
            return invited;
        }""",
    ),
    12: (
        """public Object operation1(Map<String, String> request) {
            String expression = "order by " + request.get("sort");
            return orders.search(context.tenantId(), expression, 0, 100);
        }""",
        """public Object operation2(Map<String, String> request) {
            int page = Integer.parseInt(request.get("page"));
            int size = Integer.parseInt(request.get("size"));
            int offset = page * size;
            if (size > 500) throw new IllegalArgumentException("size");
            return orders.search(context.tenantId(), "created_at desc", offset, size);
        }""",
        """public Object operation3(Map<String, String> request) {
            List<Order> scoped = orders.search(context.tenantId(), request.get("query"), 0, 50);
            return scoped.isEmpty() ? orders.search("", request.get("query"), 0, 50) : scoped;
        }""",
    ),
    13: (
        """public Object operation1(Map<String, String> request) {
            Path root = files.exportRoot(context.tenantId()).resolve("imports");
            Path target = root.resolve(request.get("entryName"));
            return target.toString();
        }""",
        """public Object operation2(Map<String, String> request) {
            List<String> rows = Arrays.asList(request.get("rows").split("\\\\|"));
            int saved = 0;
            for (String row : rows) {
                try {
                    events.publish("catalog.import", context.tenantId(), row);
                    saved++;
                } catch (RuntimeException failure) {
                    audit.record(context.tenantId(), "IMPORT_ROW_FAILED", row);
                }
            }
            return saved;
        }""",
        """public Object operation3(Map<String, String> request) {
            return request.get("row") + "," + request.get("error") + System.lineSeparator();
        }""",
    ),
    14: (
        """public Object operation1(Map<String, String> request) {
            String key = "product:" + request.get("productId");
            return cache.get(key).orElseGet(() -> request.get("productJson"));
        }""",
        """public Object operation2(Map<String, String> request) {
            String key = "product:" + context.tenantId() + ":" + request.get("productId");
            cache.evict(key);
            events.publish("catalog.update", request.get("productId"), request.get("productJson"));
            return "updated";
        }""",
        """public Object operation3(Map<String, String> request) {
            String key = "product:" + context.tenantId() + ":" + request.get("productId");
            events.publish("catalog.created", request.get("productId"), request.get("productJson"));
            return cache.get(key).orElse("NOT_FOUND");
        }""",
    ),
    15: (
        """public Object operation1(Map<String, String> request) {
            String clientIp = request.get("xForwardedFor").split(",")[0].trim();
            return cache.increment("login:" + clientIp, Duration.ofMinutes(1));
        }""",
        """public Object operation2(Map<String, String> request) {
            String key = "login:" + request.get("username");
            long current = cache.get(key).map(Long::parseLong).orElse(0L);
            if (current >= 5) throw new SecurityException("locked");
            cache.increment(key, Duration.ofMinutes(10));
            return current + 1;
        }""",
        """public Object operation3(Map<String, String> request) {
            try {
                return cache.increment("login:" + request.get("username"), Duration.ofMinutes(10));
            } catch (RuntimeException unavailable) {
                audit.record(context.tenantId(), "RATE_LIMIT_UNAVAILABLE", request.get("username"));
                return 0L;
            }
        }""",
    ),
    16: (
        """public Object operation1(Map<String, String> request) {
            SpelExpressionParser parser = new SpelExpressionParser();
            return parser.parseExpression(request.get("template")).getValue(request);
        }""",
        """public Object operation2(Map<String, String> request) {
            UserAccount account = users.findById(request.get("subscriptionId")).orElseThrow();
            users.save(new UserAccount(account.id(), account.tenantId(), Set.of(), account.version()));
            return "unsubscribed";
        }""",
        """public Object operation3(Map<String, String> request) {
            String attemptId = UUID.randomUUID().toString();
            events.publish("notification.send", attemptId, request.get("message"));
            return attemptId;
        }""",
    ),
    17: (
        """public Object operation1(Map<String, String> request) {
            URI callback = URI.create(request.get("callbackUrl"));
            return http.post(callback, Map.of("orderId", request.get("orderId")),
                    Duration.ofSeconds(3), request.get("requestId"));
        }""",
        """public Object operation2(Map<String, String> request) {
            String requestId = UUID.randomUUID().toString();
            return http.post(URI.create(request.get("carrierUrl")), Map.of("orderId", request.get("orderId")),
                    Duration.ofSeconds(5), requestId);
        }""",
        """public Object operation3(Map<String, String> request) {
            long timeoutSeconds = Long.parseLong(request.get("timeoutSeconds"));
            return http.post(URI.create(request.get("carrierUrl")), Map.of("orderId", request.get("orderId")),
                    Duration.ofMillis(timeoutSeconds), request.get("requestId"));
        }""",
    ),
    18: (
        """public Object operation1(Map<String, String> request) {
            return orders.search(request.get("tenantId"), "created_at", 0, Integer.MAX_VALUE);
        }""",
        """public Object operation2(Map<String, String> request) {
            List<Order> all = orders.search(context.tenantId(), "created_at", 0, Integer.MAX_VALUE);
            return all.stream().map(Order::id).collect(Collectors.joining(","));
        }""",
        """public Object operation3(Map<String, String> request) {
            return request.get("orderId") + "," + request.get("customerNote") + System.lineSeparator();
        }""",
    ),
    19: (
        """public Object operation1(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            if (OrderStatus.valueOf(request.get("status")).ordinal() > OrderStatus.valueOf(order.status()).ordinal()) {
                order.status(request.get("status"));
                orders.save(order);
            }
            return order;
        }""",
        """public Object operation2(Map<String, String> request) {
            Order current = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            Order mapped = new Order(current.id(), current.tenantId(), current.total(), request.get("status"));
            orders.save(mapped);
            return mapped;
        }""",
        """public Object operation3(Map<String, String> request) {
            Order order = orders.findByTenantAndId(context.tenantId(), request.get("orderId")).orElseThrow();
            order.version(order.version() + 1);
            order.status(request.get("status"));
            orders.save(order);
            events.publish("order.compensated", order.id(), Long.toString(order.version()));
            return order;
        }""",
    ),
    20: (
        """public Object operation1(Map<String, String> request) {
            String tenantSecret = cache.get("config:" + context.tenantId() + ":secret")
                    .orElse(request.get("globalSecret"));
            return tenantSecret;
        }""",
        """public Object operation2(Map<String, String> request) {
            runtimeConfig.clear();
            runtimeConfig.putAll(request);
            return runtimeConfig.size();
        }""",
        """public Object operation3(Map<String, String> request) {
            runtimeConfig = new HashMap<>();
            runtimeConfig.put("tenant", context.tenantId());
            runtimeConfig.put("paymentUrl", request.get("paymentUrl"));
            runtimeConfig.put("secret", request.get("secret"));
            return runtimeConfig;
        }""",
    ),
}


SCENARIOS.extend(_more_scenarios())


def _normalise(text: str) -> str:
    return dedent(text).strip() + "\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_normalise(content), encoding="utf-8", newline="\n")


def _materialize_baseline() -> None:
    if BASELINE.exists():
        shutil.rmtree(BASELINE)
    for relative, content in BASE_FILES.items():
        module = relative.split("/", 1)[0]
        if relative.endswith("/pom.xml") and "MODULE_NAME" in content:
            content = content.replace("MODULE_NAME", module)
        _write(BASELINE / relative, content)


def _service_source(scenario: Scenario) -> str:
    methods = _indent(scenario.methods, 4)
    return f"""package com.tradeflow.application.feature;

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
public final class {scenario.class_name}Service {{
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

    public {scenario.class_name}Service(
            OrderRepository orders, InventoryRepository inventory,
            PaymentGateway payments, EventPublisher events, CacheStore cache,
            ExternalHttpClient http, FileStore files, OutboxRepository outbox,
            UserRepository users, AuditSink audit, TenantContext context) {{
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
    }}

{methods}

    private enum OrderStatus {{
        CREATED, PAID, FULFILLING, SHIPPED, CANCELLED
    }}
}}
"""


def _coordinator_source(scenario: Scenario) -> str:
    methods = "\n\n".join(
        f"""    public Object {name}(Map<String, String> request) {{
        audit.record(context.tenantId(), "{scenario.slug.upper()}", "{name}");
        return service.{name}(request);
    }}"""
        for name in scenario.method_names
    )
    return f"""package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class {scenario.class_name}Coordinator {{
    private final {scenario.class_name}Service service;
    private final AuditSink audit;
    private final TenantContext context;

    public {scenario.class_name}Coordinator(
            {scenario.class_name}Service service, AuditSink audit, TenantContext context) {{
        this.service = service;
        this.audit = audit;
        this.context = context;
    }}

{methods}
}}
"""


def _controller_source(scenario: Scenario) -> str:
    endpoints = "\n\n".join(
        f"""    @PostMapping("/{index}")
    public ResponseEntity<Object> {name}(@RequestBody Map<String, String> request) {{
        return ResponseEntity.ok(coordinator.{name}(request));
    }}"""
        for index, name in enumerate(scenario.method_names, 1)
    )
    return f"""package com.tradeflow.web.feature;

import com.tradeflow.application.feature.{scenario.class_name}Coordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/{scenario.slug}")
public final class {scenario.class_name}Controller {{
    private final {scenario.class_name}Coordinator coordinator;

    public {scenario.class_name}Controller({scenario.class_name}Coordinator coordinator) {{
        this.coordinator = coordinator;
    }}

{endpoints}
}}
"""


def _worker_source(scenario: Scenario) -> str:
    return f"""package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.{scenario.class_name}Coordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class {scenario.class_name}Worker implements WorkerMarker {{
    private final {scenario.class_name}Coordinator coordinator;

    public {scenario.class_name}Worker({scenario.class_name}Coordinator coordinator) {{
        this.coordinator = coordinator;
    }}

    public Object execute(String operation, Map<String, String> payload) {{
        return switch (operation) {{
            case "one" -> coordinator.{scenario.method_names[0]}(payload);
            case "two" -> coordinator.{scenario.method_names[1]}(payload);
            case "three" -> coordinator.{scenario.method_names[2]}(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        }};
    }}

    @Override
    public String workerName() {{
        return "{scenario.slug}";
    }}
}}
"""


def _indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else "" for line in text.splitlines())


def _scenario_files(scenario: Scenario) -> dict[str, str]:
    return {
        (
            "tradeflow-application/src/main/java/com/tradeflow/application/feature/"
            f"{scenario.class_name}Service.java"
        ): _service_source(scenario),
        (
            "tradeflow-application/src/main/java/com/tradeflow/application/feature/"
            f"{scenario.class_name}Coordinator.java"
        ): _coordinator_source(scenario),
        (
            "tradeflow-web/src/main/java/com/tradeflow/web/feature/"
            f"{scenario.class_name}Controller.java"
        ): _controller_source(scenario),
        (
            "tradeflow-worker/src/main/java/com/tradeflow/worker/feature/"
            f"{scenario.class_name}Worker.java"
        ): _worker_source(scenario),
    }


def _diff_for(files: dict[str, str]) -> str:
    parts: list[str] = []
    for relative, content in sorted(files.items()):
        new_lines = _normalise(content).splitlines(keepends=True)
        parts.extend(difflib.unified_diff(
            [],
            new_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="\n",
        ))
    return "".join(parts)


def _line_for(source: Path, method_name: str) -> int:
    for line_no, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if f" {method_name}(" in line:
            return line_no + 1
    raise ValueError(f"{method_name} not found in {source}")


def _oracle_source(scenario: Scenario, issue_spec: IssueSpec, index: int) -> str:
    return f"""
        package com.tradeflow.oracle;

        import static org.junit.jupiter.api.Assertions.assertEquals;
        import org.junit.jupiter.api.Test;

        /**
         * Evaluation-only oracle for {scenario.case_id}/{issue_spec.slug}.
         * Install this source in the isolated oracle harness; it is intentionally
         * excluded from the reviewed repository snapshot.
         */
        final class {scenario.class_name}{index}OracleTest {{
            @Test
            void {issue_spec.slug}_preserves_the_business_invariant() {{
                OracleResult result = TradeFlowOracleHarness.run(
                        "{scenario.case_id}", "{issue_spec.slug}");
                assertEquals("{issue_spec.consequence}", result.observedFailure());
            }}
        }}
    """


def _case_yaml(scenario: Scenario, source_relative: str, source: Path) -> dict:
    expected = []
    for index, spec in enumerate(scenario.issues, 1):
        expected.append({
            "id": f"{scenario.case_id}-issue-{index}",
            "type_keywords": list(spec.keywords),
            "file": f"{scenario.class_name}Service.java",
            "line": _line_for(source, spec.slug),
            "tolerance": 5,
            "severity": spec.severity,
            "note": spec.title,
            "root_cause": spec.root_cause,
            "risk_tag": spec.primary,
            "evidence_anchors": [
                f"{scenario.class_name}Controller",
                f"{scenario.class_name}Coordinator",
                f"{scenario.class_name}Service.{spec.slug}",
            ],
        })
    return {
        "id": scenario.case_id,
        "category": scenario.title,
        "dimension": scenario.dimension,
        "language": "java",
        "capability": list(scenario.capability),
        "description": (
            f"TradeFlow 完整项目中的{scenario.title} PR；包含三个需要跨层调用链、"
            "状态或外部副作用语义才能确认的独立问题。"
        ),
        "expected": expected,
        "provenance": {
            "source": "Codeguard controlled project-level benchmark",
            "repository_url": "",
            "base_revision": "tradeflow-clean-v1",
            "head_revision": scenario.case_id,
            "patch_direction": "seeded-pr",
            "license": "CC0-1.0 benchmark fixture",
        },
    }


def _ground_truth(
    scenario: Scenario, source_relative: str, source: Path
) -> dict:
    issues = []
    for index, spec in enumerate(scenario.issues, 1):
        method_line = _line_for(source, spec.slug)
        oracle_name = f"{scenario.class_name}{index}OracleTest.java"
        issues.append({
            "id": f"{scenario.case_id}-issue-{index}",
            "title": spec.title,
            "dimension": scenario.dimension,
            "primary_risk_tag": spec.primary,
            "secondary_risk_tags": list(spec.secondary),
            "expected_reviewers": list(spec.reviewers),
            "required_knowledge": list(spec.knowledge),
            "risk_coverage": spec.coverage,
            "routing_hazard": spec.routing_hazard,
            "taxonomy_gap": spec.taxonomy_gap,
            "root_cause": spec.root_cause,
            "trigger": spec.trigger,
            "observable_consequence": spec.consequence,
            "file": source_relative,
            "line": method_line,
            "expected_trigger_lines": [method_line],
            "call_path": [
                f"{scenario.class_name}Controller.{spec.slug}",
                f"{scenario.class_name}Coordinator.{spec.slug}",
                f"{scenario.class_name}Service.{spec.slug}",
                _sink_for(spec.primary),
            ],
            "evidence_anchors": [
                f"{scenario.class_name}Service.{spec.slug}",
                _sink_for(spec.primary),
            ],
            "fix_location": f"{scenario.class_name}Service.{spec.slug}",
            "fix_action": spec.fix_action,
            "why_independent": (
                "该主张拥有独立触发条件、可观察后果和修复动作；修复同 PR 的其他"
                "两条主张不会消除本问题。"
            ),
            "severity": spec.severity,
            "severity_rationale": spec.consequence,
            "oracle_test": oracle_name,
            "review_visibility": {
                "repo_snapshot": True,
                "oracle_test": False,
                "ground_truth": False,
            },
        })
    return {
        "case_id": scenario.case_id,
        "project": "TradeFlow",
        "baseline": "tradeflow-clean-v1",
        "issues": issues,
    }


def _sink_for(tag: str) -> str:
    return {
        "AUTHORIZATION": "OrderRepository.findByTenantAndId",
        "SQL_DATA_ACCESS": "OrderRepository.search",
        "TRANSACTION_ATOMICITY": "OrderRepository.save",
        "CONCURRENCY_CONSISTENCY": "OrderRepository.saveIfVersion",
        "IDEMPOTENCY_RETRY": "CacheStore.put",
        "CACHE_CONSISTENCY": "CacheStore.get",
        "MESSAGE_DELIVERY": "EventPublisher.publish",
        "ERROR_HANDLING": "message acknowledgement",
        "FILE_PATH_IO": "FileStore.open",
        "SSRF_OUTBOUND": "ExternalHttpClient.post",
        "INJECTION": "expression/query interpreter",
        "PERFORMANCE": "unbounded repository/client operation",
        "AUTHENTICATION_SESSION": "authentication decision",
        "INPUT_VALIDATION": "sensitive business operation",
        "CONFIG_SECURITY": "tenant runtime configuration",
        "API_CONTRACT": "domain invariant",
    }.get(tag, "application side effect")


def _materialize_case(scenario: Scenario) -> None:
    case_dir = CASES / scenario.case_id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    snapshot = case_dir / "repo"
    shutil.copytree(BASELINE, snapshot)
    files = _scenario_files(scenario)
    for relative, content in files.items():
        _write(snapshot / relative, content)
    _write(case_dir / "changes.diff", _diff_for(files))

    source_relative = (
        "tradeflow-application/src/main/java/com/tradeflow/application/feature/"
        f"{scenario.class_name}Service.java"
    )
    source = snapshot / source_relative
    (case_dir / "case.yaml").write_text(
        yaml.safe_dump(
            _case_yaml(scenario, source_relative, source),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (case_dir / "ground-truth.yaml").write_text(
        yaml.safe_dump(
            _ground_truth(scenario, source_relative, source),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    for index, spec in enumerate(scenario.issues, 1):
        _write(
            case_dir / "oracle-tests" / f"{scenario.class_name}{index}OracleTest.java",
            _oracle_source(scenario, spec, index),
        )


def _write_manifest() -> None:
    coverage = {"exact": 0, "composite": 0, "gap": 0}
    for scenario in SCENARIOS:
        for spec in scenario.issues:
            coverage[spec.coverage] += 1
    manifest = {
        "id": "complex-pr-v1",
        "project": "TradeFlow",
        "benchmark_type": "controlled-project-level",
        "language": "java",
        "java_version": 17,
        "framework": "Spring Boot",
        "baseline": "tradeflow-clean-v1",
        "case_count": len(SCENARIOS),
        "issue_count": sum(len(item.issues) for item in SCENARIOS),
        "risk_coverage": coverage,
        "review_visibility": {
            "included": ["repo", "changes.diff"],
            "excluded": ["ground-truth.yaml", "oracle-tests"],
        },
        "cases": [
            {
                "id": scenario.case_id,
                "title": scenario.title,
                "issues": len(scenario.issues),
                "capability": list(scenario.capability),
            }
            for scenario in SCENARIOS
        ],
    }
    (ROOT / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


def _write_readme() -> None:
    _write(
        ROOT / "README.md",
        """
        # TradeFlow Complex PR Benchmark v1

        `complex-pr-v1` is a controlled project-level Java code-review dataset.
        It is not a collection of real open-source PRs. A clean, complete
        seven-module Spring Boot project is used as the common baseline, and
        every case contains a fully materialized independent PR snapshot.

        ## Contract

        - 20 independent PR cases and exactly 3 gold issues per PR.
        - Every reviewed snapshot contains all seven Maven modules.
        - `changes.diff` is the only review patch.
        - `ground-truth.yaml` and `oracle-tests/` are evaluator-only and must
          never be mounted into the Gateway project snapshot.
        - Risk/knowledge coverage is frozen at 36 exact, 16 composite and 8 gap
          issues. Gap names are annotation vocabulary, not production RiskTags.

        ## Rebuilding

        Run `python build_dataset.py` from this directory to deterministically
        rebuild the baseline and all committed snapshots. Rebuilding does not
        invoke Codeguard, an LLM, Maven, or the oracle tests.
        """,
    )


def main() -> None:
    _materialize_baseline()
    if CASES.exists():
        shutil.rmtree(CASES)
    CASES.mkdir(parents=True)
    for scenario in sorted(SCENARIOS, key=lambda item: item.number):
        _materialize_case(scenario)
    _write_manifest()
    _write_readme()


if __name__ == "__main__":
    main()

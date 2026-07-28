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

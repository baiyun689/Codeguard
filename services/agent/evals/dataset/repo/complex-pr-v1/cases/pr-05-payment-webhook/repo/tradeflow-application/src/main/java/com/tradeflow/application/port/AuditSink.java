package com.tradeflow.application.port;

public interface AuditSink {
    void record(String tenantId, String action, String detail);
}

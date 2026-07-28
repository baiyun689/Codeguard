package com.tradeflow.application.security;

public interface TenantContext {
    String tenantId();
    String userId();
    boolean hasRole(String role);
}

package com.tradeflow.domain;

import java.util.Set;

public record UserAccount(String id, String tenantId, Set<String> roles, long version) {
    public boolean hasRole(String role) { return roles.contains(role); }
}

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

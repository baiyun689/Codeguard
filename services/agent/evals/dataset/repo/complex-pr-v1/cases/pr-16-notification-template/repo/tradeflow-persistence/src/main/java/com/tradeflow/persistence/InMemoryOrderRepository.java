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

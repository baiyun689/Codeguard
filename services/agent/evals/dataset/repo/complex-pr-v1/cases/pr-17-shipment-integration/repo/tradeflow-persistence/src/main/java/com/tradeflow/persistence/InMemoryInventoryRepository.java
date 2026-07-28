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

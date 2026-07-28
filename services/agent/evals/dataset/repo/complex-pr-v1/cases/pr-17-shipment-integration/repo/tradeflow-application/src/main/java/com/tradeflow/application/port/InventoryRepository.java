package com.tradeflow.application.port;

import com.tradeflow.domain.InventoryItem;
import java.util.Optional;

public interface InventoryRepository {
    Optional<InventoryItem> findBySku(String sku);
    void save(InventoryItem item);
    boolean reserveIfAvailable(String sku, int quantity, long version);
}

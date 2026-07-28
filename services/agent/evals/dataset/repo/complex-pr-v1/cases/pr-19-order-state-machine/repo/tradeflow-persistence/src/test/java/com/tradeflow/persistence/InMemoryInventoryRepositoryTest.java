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

package com.demo.inventory;

import java.util.HashMap;
import java.util.Map;

public class InventoryService {

    private final Map<String, Integer> stock = new HashMap<>();

    public void set(String sku, int qty) {
        stock.put(sku, qty);
    }

    public int quantity(String sku) {
        return stock.getOrDefault(sku, 0);
    }
}

package com.tradeflow.domain;

public final class InventoryItem {
    private final String sku;
    private int available;
    private long version;

    public InventoryItem(String sku, int available) {
        this.sku = sku;
        this.available = available;
    }

    public String sku() { return sku; }
    public int available() { return available; }
    public long version() { return version; }
    public void available(int value) { available = value; }
    public void version(long value) { version = value; }
}

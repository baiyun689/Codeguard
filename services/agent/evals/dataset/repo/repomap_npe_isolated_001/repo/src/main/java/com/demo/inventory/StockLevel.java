package com.demo.inventory;

public class StockLevel {

    private final int onHand;
    private final int reserved;

    public StockLevel(int onHand, int reserved) {
        this.onHand = onHand;
        this.reserved = reserved;
    }

    public int available() {
        return Math.max(0, onHand - reserved);
    }
}

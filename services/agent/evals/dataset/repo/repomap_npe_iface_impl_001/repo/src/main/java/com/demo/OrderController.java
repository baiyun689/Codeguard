package com.demo;

public class OrderController {

    private final OrderStore store;

    public OrderController(OrderStore store) {
        this.store = store;
    }

    public String codeOf(String id) {
        return store.lookup(id).trim();
    }
}

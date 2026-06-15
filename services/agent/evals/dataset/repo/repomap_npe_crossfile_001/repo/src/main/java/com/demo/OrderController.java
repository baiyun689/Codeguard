package com.demo;

public class OrderController {

    private final OrderRepository repository;

    public OrderController(OrderRepository repository) {
        this.repository = repository;
    }

    public String codeOf(String id) {
        return repository.findCode(id).trim();
    }
}

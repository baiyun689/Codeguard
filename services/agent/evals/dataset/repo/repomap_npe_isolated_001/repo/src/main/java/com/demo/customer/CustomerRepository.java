package com.demo.customer;

import java.util.HashMap;
import java.util.Map;

public class CustomerRepository {

    private final Map<String, String> names = new HashMap<>();

    public void save(String id, String name) {
        names.put(id, name);
    }

    public String findById(String id) {
        return names.getOrDefault(id, "GUEST");
    }
}

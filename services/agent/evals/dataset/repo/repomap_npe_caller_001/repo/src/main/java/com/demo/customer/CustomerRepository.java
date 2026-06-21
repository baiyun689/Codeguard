package com.demo.customer;

import java.util.HashMap;
import java.util.Map;

public class CustomerRepository {

    private final Map<String, String> emails = new HashMap<>();

    public void save(String id, String email) {
        emails.put(id, email);
    }

    public String emailOf(String id) {
        return emails.getOrDefault(id, "none@example.com");
    }
}

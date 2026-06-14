package com.demo;

import java.util.HashMap;
import java.util.Map;

public class UserLookup {

    private final Map<String, String> idByName = new HashMap<>();

    public String idOf(String name) {
        return find(name).trim();
    }

    private String find(String name) {
        return idByName.get(name);
    }
}

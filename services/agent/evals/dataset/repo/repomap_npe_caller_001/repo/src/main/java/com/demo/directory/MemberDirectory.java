package com.demo.directory;

import java.util.HashMap;
import java.util.Map;

public class MemberDirectory {

    private final Map<String, String> cache = new HashMap<>();

    public void register(String id, String name) {
        cache.put(id, name);
    }

    public String displayName(String id) {
        return cache.get(id);
    }
}

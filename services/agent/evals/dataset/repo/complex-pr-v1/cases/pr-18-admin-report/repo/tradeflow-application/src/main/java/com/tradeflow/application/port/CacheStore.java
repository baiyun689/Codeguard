package com.tradeflow.application.port;

import java.time.Duration;
import java.util.Optional;

public interface CacheStore {
    Optional<String> get(String key);
    void put(String key, String value, Duration ttl);
    void evict(String key);
    long increment(String key, Duration ttl);
}

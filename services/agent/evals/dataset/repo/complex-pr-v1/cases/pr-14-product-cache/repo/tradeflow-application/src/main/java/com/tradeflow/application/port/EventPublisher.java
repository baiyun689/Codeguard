package com.tradeflow.application.port;

public interface EventPublisher {
    void publish(String topic, String key, String payload);
}

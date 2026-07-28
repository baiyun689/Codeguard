package com.tradeflow.integrations;

import com.tradeflow.application.port.EventPublisher;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import org.springframework.stereotype.Component;

@Component
public final class RecordingEventPublisher implements EventPublisher {
    private final List<String> events = new CopyOnWriteArrayList<>();
    public void publish(String topic, String key, String payload) {
        events.add(topic + ":" + key + ":" + payload);
    }
    public List<String> events() { return List.copyOf(events); }
}

package com.tradeflow.application.port;

import com.tradeflow.domain.OutboxEvent;
import java.time.Instant;
import java.util.List;

public interface OutboxRepository {
    List<OutboxEvent> findReady(Instant now, int limit);
    void save(OutboxEvent event);
    boolean claim(String eventId, String workerId);
}

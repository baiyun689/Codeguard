package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.OrderSearchCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class OrderSearchWorker implements WorkerMarker {
    private final OrderSearchCoordinator coordinator;

    public OrderSearchWorker(OrderSearchCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "search-with-sort" ->
                    coordinator.searchWithSort(payload);
            case "search-page" ->
                    coordinator.searchPage(payload);
            case "search-with-fallback" ->
                    coordinator.searchWithFallback(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "order-search";
    }
}

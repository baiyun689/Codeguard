package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.ProductCacheCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class ProductCacheWorker implements WorkerMarker {
    private final ProductCacheCoordinator coordinator;

    public ProductCacheWorker(ProductCacheCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.loadProduct(payload);
            case "two" -> coordinator.updateProduct(payload);
            case "three" -> coordinator.createProduct(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "product-cache";
    }
}

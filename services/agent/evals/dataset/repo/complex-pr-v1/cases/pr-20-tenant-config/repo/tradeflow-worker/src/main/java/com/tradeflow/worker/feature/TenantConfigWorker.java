package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.TenantConfigCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class TenantConfigWorker implements WorkerMarker {
    private final TenantConfigCoordinator coordinator;

    public TenantConfigWorker(TenantConfigCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "load-tenant-secret" ->
                    coordinator.loadTenantSecret(payload);
            case "reload-configuration" ->
                    coordinator.reloadConfiguration(payload);
            case "publish-configuration" ->
                    coordinator.publishConfiguration(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "tenant-config";
    }
}

package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.JwtKeyRotationCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class JwtKeyRotationWorker implements WorkerMarker {
    private final JwtKeyRotationCoordinator coordinator;

    public JwtKeyRotationWorker(JwtKeyRotationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.loadSigningKey(payload);
            case "two" -> coordinator.isTokenActive(payload);
            case "three" -> coordinator.loadTenantRoles(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "jwt-key-rotation";
    }
}

package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.LoginRateLimitCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class LoginRateLimitWorker implements WorkerMarker {
    private final LoginRateLimitCoordinator coordinator;

    public LoginRateLimitWorker(LoginRateLimitCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.countByClientAddress(payload);
            case "two" -> coordinator.recordLoginFailure(payload);
            case "three" -> coordinator.checkRateLimit(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "login-rate-limit";
    }
}

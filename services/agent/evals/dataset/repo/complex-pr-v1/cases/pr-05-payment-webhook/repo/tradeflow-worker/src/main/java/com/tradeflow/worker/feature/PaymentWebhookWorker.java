package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.PaymentWebhookCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class PaymentWebhookWorker implements WorkerMarker {
    private final PaymentWebhookCoordinator coordinator;

    public PaymentWebhookWorker(PaymentWebhookCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.verifyWebhookSignature(payload);
            case "two" -> coordinator.acceptWebhookEvent(payload);
            case "three" -> coordinator.applyPaymentEvent(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "payment-webhook";
    }
}

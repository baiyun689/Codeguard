package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.RefundFlowCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class RefundFlowWorker implements WorkerMarker {
    private final RefundFlowCoordinator coordinator;

    public RefundFlowWorker(RefundFlowCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "refund-against-order" ->
                    coordinator.refundAgainstOrder(payload);
            case "refund-remaining-amount" ->
                    coordinator.refundRemainingAmount(payload);
            case "refund-converted-amount" ->
                    coordinator.refundConvertedAmount(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "refund-flow";
    }
}

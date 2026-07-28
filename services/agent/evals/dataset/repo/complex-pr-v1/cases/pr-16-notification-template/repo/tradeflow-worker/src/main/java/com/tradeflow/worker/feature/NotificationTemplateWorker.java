package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.NotificationTemplateCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class NotificationTemplateWorker implements WorkerMarker {
    private final NotificationTemplateCoordinator coordinator;

    public NotificationTemplateWorker(NotificationTemplateCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.renderNotification(payload);
            case "two" -> coordinator.unsubscribeRecipient(payload);
            case "three" -> coordinator.sendNotification(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "notification-template";
    }
}

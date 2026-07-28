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
            case "render-notification" ->
                    coordinator.renderNotification(payload);
            case "unsubscribe-recipient" ->
                    coordinator.unsubscribeRecipient(payload);
            case "send-notification" ->
                    coordinator.sendNotification(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "notification-template";
    }
}

package com.tradeflow.web.feature;

import com.tradeflow.application.feature.NotificationTemplateCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/notification-template")
public final class NotificationTemplateController {
    private final NotificationTemplateCoordinator coordinator;

    public NotificationTemplateController(NotificationTemplateCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/render-notification")
    public ResponseEntity<Object> renderNotification(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.renderNotification(request));
    }

    @PostMapping("/unsubscribe-recipient")
    public ResponseEntity<Object> unsubscribeRecipient(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.unsubscribeRecipient(request));
    }

    @PostMapping("/send-notification")
    public ResponseEntity<Object> sendNotification(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.sendNotification(request));
    }
}

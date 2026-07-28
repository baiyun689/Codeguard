package com.tradeflow.web.feature;

import com.tradeflow.application.feature.OrderSearchCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/order-search")
public final class OrderSearchController {
    private final OrderSearchCoordinator coordinator;

    public OrderSearchController(OrderSearchCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/search-with-sort")
    public ResponseEntity<Object> searchWithSort(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.searchWithSort(request));
    }

    @PostMapping("/search-page")
    public ResponseEntity<Object> searchPage(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.searchPage(request));
    }

    @PostMapping("/search-with-fallback")
    public ResponseEntity<Object> searchWithFallback(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.searchWithFallback(request));
    }
}

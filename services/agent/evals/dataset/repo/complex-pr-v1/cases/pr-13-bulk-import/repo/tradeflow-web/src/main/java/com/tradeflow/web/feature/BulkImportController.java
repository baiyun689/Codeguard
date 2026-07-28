package com.tradeflow.web.feature;

import com.tradeflow.application.feature.BulkImportCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/bulk-import")
public final class BulkImportController {
    private final BulkImportCoordinator coordinator;

    public BulkImportController(BulkImportCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> resolveArchiveEntry(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.resolveArchiveEntry(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> importRows(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.importRows(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> renderImportError(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.renderImportError(request));
    }
}

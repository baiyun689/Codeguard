package com.tradeflow.web.feature;

import com.tradeflow.application.feature.FileExportCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/file-export")
public final class FileExportController {
    private final FileExportCoordinator coordinator;

    public FileExportController(FileExportCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/open-export")
    public ResponseEntity<Object> openExport(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.openExport(request));
    }

    @PostMapping("/open-owned-export")
    public ResponseEntity<Object> openOwnedExport(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.openOwnedExport(request));
    }

    @PostMapping("/open-verified-export")
    public ResponseEntity<Object> openVerifiedExport(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.openVerifiedExport(request));
    }
}

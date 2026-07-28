package com.tradeflow.web.feature;

import com.tradeflow.application.feature.ProductCacheCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/product-cache")
public final class ProductCacheController {
    private final ProductCacheCoordinator coordinator;

    public ProductCacheController(ProductCacheCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/load-product")
    public ResponseEntity<Object> loadProduct(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.loadProduct(request));
    }

    @PostMapping("/update-product")
    public ResponseEntity<Object> updateProduct(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.updateProduct(request));
    }

    @PostMapping("/create-product")
    public ResponseEntity<Object> createProduct(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.createProduct(request));
    }
}

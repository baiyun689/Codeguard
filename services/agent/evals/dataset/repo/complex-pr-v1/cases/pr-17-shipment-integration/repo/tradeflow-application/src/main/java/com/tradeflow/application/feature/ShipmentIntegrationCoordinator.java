package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class ShipmentIntegrationCoordinator {
    private final ShipmentIntegrationService service;
    private final AuditSink audit;
    private final TenantContext context;

    public ShipmentIntegrationCoordinator(
            ShipmentIntegrationService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object createShipment(Map<String, String> request) {
        audit.record(context.tenantId(), "SHIPMENT-INTEGRATION", "createShipment");
        return service.createShipment(request);
    }

    public Object retryShipment(Map<String, String> request) {
        audit.record(context.tenantId(), "SHIPMENT-INTEGRATION", "retryShipment");
        return service.retryShipment(request);
    }

    public Object createShipmentWithTimeout(Map<String, String> request) {
        audit.record(context.tenantId(), "SHIPMENT-INTEGRATION", "createShipmentWithTimeout");
        return service.createShipmentWithTimeout(request);
    }
}

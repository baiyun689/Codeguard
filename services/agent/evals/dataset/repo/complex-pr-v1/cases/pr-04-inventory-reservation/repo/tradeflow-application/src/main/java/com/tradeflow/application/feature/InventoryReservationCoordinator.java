package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class InventoryReservationCoordinator {
    private final InventoryReservationService service;
    private final AuditSink audit;
    private final TenantContext context;

    public InventoryReservationCoordinator(
            InventoryReservationService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object reserveStock(Map<String, String> request) {
        audit.record(context.tenantId(), "INVENTORY-RESERVATION", "reserveStock");
        return service.reserveStock(request);
    }

    public Object reserveWithLocalLock(Map<String, String> request) {
        audit.record(context.tenantId(), "INVENTORY-RESERVATION", "reserveWithLocalLock");
        return service.reserveWithLocalLock(request);
    }

    public Object releaseExpiredReservation(Map<String, String> request) {
        audit.record(context.tenantId(), "INVENTORY-RESERVATION", "releaseExpiredReservation");
        return service.releaseExpiredReservation(request);
    }
}

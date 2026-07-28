package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class CouponPricingCoordinator {
    private final CouponPricingService service;
    private final AuditSink audit;
    private final TenantContext context;

    public CouponPricingCoordinator(
            CouponPricingService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object calculateCombinedDiscount(Map<String, String> request) {
        audit.record(context.tenantId(), "COUPON-PRICING", "calculateCombinedDiscount");
        return service.calculateCombinedDiscount(request);
    }

    public Object applyCouponRules(Map<String, String> request) {
        audit.record(context.tenantId(), "COUPON-PRICING", "applyCouponRules");
        return service.applyCouponRules(request);
    }

    public Object loadCustomerPrice(Map<String, String> request) {
        audit.record(context.tenantId(), "COUPON-PRICING", "loadCustomerPrice");
        return service.loadCustomerPrice(request);
    }
}

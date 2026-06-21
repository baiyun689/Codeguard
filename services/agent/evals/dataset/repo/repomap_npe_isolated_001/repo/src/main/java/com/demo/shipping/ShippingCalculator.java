package com.demo.shipping;

import java.util.HashMap;
import java.util.Map;

/**
 * 运费计算:也带一个 lookup(String) —— 与 PriceCatalog.lookup 同名,是制造"按名匹配有歧义"
 * 的诱饵;但它返回基本类型、且与 QuoteController 无引用关系,与本案缺陷无关。
 */
public class ShippingCalculator {

    private final Map<String, Double> rates = new HashMap<>();

    public double lookup(String zone) {
        return rates.getOrDefault(zone, 9.99);
    }

    public double withSurcharge(String zone, double surcharge) {
        return lookup(zone) + surcharge;
    }
}

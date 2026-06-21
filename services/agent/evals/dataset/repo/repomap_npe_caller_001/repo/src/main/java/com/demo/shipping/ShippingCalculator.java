package com.demo.shipping;

import java.util.HashMap;
import java.util.Map;

public class ShippingCalculator {

    private final Map<String, Double> rates = new HashMap<>();

    public double rateFor(String zone) {
        return rates.getOrDefault(zone, 9.99);
    }
}

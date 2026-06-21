package com.demo.tax;

import java.util.HashMap;
import java.util.Map;

public class TaxRuleSet {

    private final Map<String, Double> rules = new HashMap<>();

    public void put(String region, double rate) {
        rules.put(region, rate);
    }

    public double resolve(String region) {
        return rules.getOrDefault(region, 0.0);
    }
}

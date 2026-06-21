package com.demo.report;

import java.util.List;

public class SalesReport {

    private final List<Double> amounts;

    public SalesReport(List<Double> amounts) {
        this.amounts = amounts;
    }

    public double total() {
        double sum = 0.0;
        for (Double amount : amounts) {
            sum += amount == null ? 0.0 : amount;
        }
        return sum;
    }
}

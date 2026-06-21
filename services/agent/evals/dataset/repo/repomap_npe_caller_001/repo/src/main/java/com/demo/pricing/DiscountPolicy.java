package com.demo.pricing;

public class DiscountPolicy {

    private final double percent;

    public DiscountPolicy(double percent) {
        this.percent = percent;
    }

    public double apply(double amount) {
        return amount * (1.0 - percent / 100.0);
    }
}

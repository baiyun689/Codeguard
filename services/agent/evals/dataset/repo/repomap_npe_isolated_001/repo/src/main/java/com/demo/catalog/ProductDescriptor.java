package com.demo.catalog;

public class ProductDescriptor {

    private final String sku;
    private final String title;

    public ProductDescriptor(String sku, String title) {
        this.sku = sku;
        this.title = title;
    }

    public String summary() {
        return sku + ": " + title;
    }
}

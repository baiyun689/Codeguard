package com.demo.web;

import com.demo.pricing.PriceCatalog;

public class QuoteController {

    private final PriceCatalog catalog;

    public QuoteController(PriceCatalog catalog) {
        this.catalog = catalog;
    }

    public String tagOf(String sku) {
        return catalog.lookup(sku).trim();
    }
}

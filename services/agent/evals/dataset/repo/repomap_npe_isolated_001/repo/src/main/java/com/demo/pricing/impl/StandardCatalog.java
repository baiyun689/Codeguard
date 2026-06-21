package com.demo.pricing.impl;

import com.demo.pricing.PriceCatalog;

/**
 * 标准价目表:始终返回非空标签(缺省回退到 SKU 本身),不会触发空指针。
 */
public class StandardCatalog implements PriceCatalog {

    @Override
    public String lookup(String sku) {
        if (sku == null || sku.isEmpty()) {
            return "UNKNOWN";
        }
        return "TAG-" + sku;
    }
}

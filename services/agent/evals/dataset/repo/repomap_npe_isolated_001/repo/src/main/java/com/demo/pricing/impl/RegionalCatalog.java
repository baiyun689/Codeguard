package com.demo.pricing.impl;

import com.demo.pricing.PriceCatalog;

/**
 * 区域价目表:按区域前缀拼标签,缺省回退到标准前缀,始终非空。
 */
public class RegionalCatalog implements PriceCatalog {

    private final String region;

    public RegionalCatalog(String region) {
        this.region = region == null ? "GLOBAL" : region;
    }

    @Override
    public String lookup(String sku) {
        String base = sku == null ? "UNKNOWN" : sku;
        return region + "-" + base;
    }
}

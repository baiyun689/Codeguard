package com.demo.pricing;

/**
 * 价目表:按 SKU 取展示标签。
 *
 * <p>契约:lookup 保证<strong>永不返回 null</strong>——SKU 未命中时返回占位标签。
 * 调用方因此可放心对返回值直接做字符串操作(如 .trim())而无需判空。
 */
public interface PriceCatalog {

    /**
     * 返回该 SKU 的展示标签。
     *
     * @param sku 商品编码
     * @return 非空展示标签;未命中时返回占位串,<strong>保证不为 null</strong>
     */
    String lookup(String sku);
}

package com.demo.pricing.legacy;

import java.util.HashMap;
import java.util.Map;

import com.demo.pricing.PriceCatalog;

/**
 * 遗留资费表:由内存 Map 支撑的旧实现。
 * 注意:lookup 直接 return map.get(sku),SKU 不在表中时返回 null —— 这<strong>违反了
 * PriceCatalog "永不返回 null" 的接口契约</strong>,也是上游 QuoteController.tagOf 调 .trim()
 * 触发 NPE 的真根因。但它藏在 legacy 包、名字也不叫 *Catalog,只看 diff、甚至只读
 * QuoteController/接口(接口契约还声称非空)都定位不到——必须靠 repo map 导航到这个具体实现,
 * 才看得见契约被违反。
 */
public class TariffLookupTable implements PriceCatalog {

    private final Map<String, String> tags = new HashMap<>();

    public void register(String sku, String tag) {
        tags.put(sku, tag);
    }

    @Override
    public String lookup(String sku) {
        return tags.get(sku);
    }
}

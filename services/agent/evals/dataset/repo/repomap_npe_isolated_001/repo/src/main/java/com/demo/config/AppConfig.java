package com.demo.config;

import com.demo.pricing.PriceCatalog;
import com.demo.pricing.legacy.TariffLookupTable;
import com.demo.web.QuoteController;

/**
 * 应用装配:把 QuoteController 实际注入的 PriceCatalog 钉死为 TariffLookupTable(遗留实现)。
 * 这是"哪个实现是 live 的"唯一线索 —— repo map 经此桥(既引用 QuoteController 又引用
 * TariffLookupTable)能把正确实现顶到前排。
 */
public class AppConfig {

    public QuoteController quoteController() {
        PriceCatalog catalog = new TariffLookupTable();
        return new QuoteController(catalog);
    }
}

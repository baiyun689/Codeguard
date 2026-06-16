package com.demo.engine;

import com.demo.RenderEngine;

import java.util.HashMap;
import java.util.Map;

/**
 * RenderEngine 的缓存实现。文件名与基类名/调用点都对不上,凭 diff 猜不到这里。
 * render 命中缓存用 Map.get —— 未命中返回 null,经 InvoiceController.headerOf().trim() 触发 NPE。
 */
public class CachedSnippetEngine extends RenderEngine {

    private final Map<String, String> cache = new HashMap<>();

    public void store(String id, String html) {
        cache.put(id, html);
    }

    @Override
    public String render(String id) {
        return cache.get(id);
    }
}

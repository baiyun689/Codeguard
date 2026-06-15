package com.demo;

import java.util.HashMap;
import java.util.Map;

public class OrderRepository {

    private final Map<String, String> codeById = new HashMap<>();

    public void register(String id, String code) {
        codeById.put(id, code);
    }

    // 找不到时返回 null —— 这是 codeOf().trim() NPE 的根因,但它在另一个文件里,
    // 只看 OrderController 的 diff 看不到。
    public String findCode(String id) {
        return codeById.get(id);
    }
}

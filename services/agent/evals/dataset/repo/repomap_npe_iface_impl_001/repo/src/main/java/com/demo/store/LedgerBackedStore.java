package com.demo.store;

import com.demo.OrderStore;

import java.util.HashMap;
import java.util.Map;

/**
 * OrderStore 的内存实现。文件名与接口名/调用点都对不上,凭 diff 里的类型名 OrderStore 猜不到这里。
 * lookup 用 Map.get 实现 —— 找不到返回 null,这正是 OrderController.codeOf().trim() 的 NPE 根因。
 */
public class LedgerBackedStore implements OrderStore {

    private final Map<String, String> codes = new HashMap<>();

    public void put(String id, String code) {
        codes.put(id, code);
    }

    @Override
    public String lookup(String id) {
        return codes.get(id);
    }
}

package com.demo.directory;

import com.demo.NameDirectory;

import java.util.HashMap;
import java.util.Map;

/**
 * NameDirectory 的静态实现。文件名与接口名/委托链都对不上,凭 diff 根本猜不到这里。
 * resolve 用 Map.get —— 找不到返回 null,经 AccountService.nameOf 透传后,
 * 最终在 AccountController.labelOf().trim() 触发 NPE。
 */
public class StaticRoster implements NameDirectory {

    private final Map<String, String> names = new HashMap<>();

    public void add(String id, String name) {
        names.put(id, name);
    }

    @Override
    public String resolve(String id) {
        return names.get(id);
    }
}

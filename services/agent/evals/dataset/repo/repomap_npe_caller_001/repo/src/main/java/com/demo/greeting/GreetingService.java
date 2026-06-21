package com.demo.greeting;

import com.demo.directory.MemberDirectory;

/**
 * 上游已存在的调用方(叶子:无人引用本类)。
 * greet 假设 displayName 永不为 null,直接 .toUpperCase() —— 本次 diff 把 displayName 改成
 * 未命中返回 null 后,这里就会 NPE。但这个受害点在另一个文件、且本文件不在 diff 内,
 * 只看 diff / 只顺着被改方法往下读都碰不到,得先知道"谁调了 displayName"才查得到。
 */
public class GreetingService {

    private final MemberDirectory directory;

    public GreetingService(MemberDirectory directory) {
        this.directory = directory;
    }

    public String greet(String id) {
        return "HELLO " + directory.displayName(id).toUpperCase();
    }
}

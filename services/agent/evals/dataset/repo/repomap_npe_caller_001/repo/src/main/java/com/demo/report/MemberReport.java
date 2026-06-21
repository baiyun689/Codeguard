package com.demo.report;

import com.demo.directory.MemberDirectory;

/**
 * displayName 的另一个调用方,但它**判了空**,是安全的对照 —— 用于逼审查员真去逐个细读
 * 调用方、区分"哪个 caller 会被改坏",而非一看到"有人调 displayName"就笼统报。
 */
public class MemberReport {

    private final MemberDirectory directory;

    public MemberReport(MemberDirectory directory) {
        this.directory = directory;
    }

    public String line(String id) {
        String name = directory.displayName(id);
        if (name == null) {
            return "(unknown)";
        }
        return name.trim();
    }
}

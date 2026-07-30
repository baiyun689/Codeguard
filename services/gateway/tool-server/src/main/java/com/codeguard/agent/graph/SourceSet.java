package com.codeguard.agent.graph;

import java.util.Locale;

/** 源码事实所属集合；TEST 事实不得单独证明生产可达性或生产影响。 */
public enum SourceSet {
    MAIN,
    TEST,
    GENERATED;

    public static SourceSet fromPath(String file) {
        String normalized = ("/" + (file == null ? "" : file))
                .replace('\\', '/')
                .toLowerCase(Locale.ROOT);
        if (normalized.contains("/src/test/")
                || normalized.contains("/src/testfixtures/")
                || normalized.contains("/src/it/")
                || normalized.contains("/test/")
                || normalized.contains("/tests/")
                || normalized.contains("/generated-test-sources/")
                || normalized.contains("/integration-test/")
                || normalized.contains("/integrationtest/")) {
            return TEST;
        }
        if (normalized.contains("/generated/")
                || normalized.contains("/generated-sources/")
                || normalized.contains("/generated-src/")) {
            return GENERATED;
        }
        return MAIN;
    }

    public boolean isTest() {
        return this == TEST;
    }
}

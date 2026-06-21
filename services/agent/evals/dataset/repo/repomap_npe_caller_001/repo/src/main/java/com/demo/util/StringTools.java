package com.demo.util;

public final class StringTools {

    private StringTools() {
    }

    public static String orEmpty(String value) {
        return value == null ? "" : value;
    }

    public static String repeat(String value, int times) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < times; i++) {
            sb.append(value);
        }
        return sb.toString();
    }
}

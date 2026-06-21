package com.demo.util;

public final class StringTools {

    private StringTools() {
    }

    public static String orEmpty(String value) {
        return value == null ? "" : value;
    }

    public static String capitalize(String value) {
        if (value == null || value.isEmpty()) {
            return value;
        }
        return Character.toUpperCase(value.charAt(0)) + value.substring(1);
    }
}

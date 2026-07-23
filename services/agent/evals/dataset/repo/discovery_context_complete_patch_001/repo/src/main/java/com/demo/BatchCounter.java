package com.demo;

import java.util.List;

public class BatchCounter {
    public int count(List<String> items) {
        int total = 0;
        for (int i = 0; i <= items.size(); i++) {
            total++;
        }
        return total;
    }
}

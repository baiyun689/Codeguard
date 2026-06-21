package com.demo.audit;

public class AuditLogger {

    public void record(String actor, String action) {
        System.out.println("[audit] " + actor + " -> " + action);
    }
}

package com.codeguard.common;

/** SLO 告警事件。 */
public record SloAlert(
    String name,
    String severity,      // 告警级别：严重或警告。
    String summary,
    String detail,
    long timestampEpochMs
) {
    public static SloAlert of(String name, String severity, String summary, String detail) {
        return new SloAlert(name, severity, summary, detail, System.currentTimeMillis());
    }
}

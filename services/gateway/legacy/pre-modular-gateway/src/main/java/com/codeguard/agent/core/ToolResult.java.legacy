package com.codeguard.agent.core;

/**
 * 工具执行结果:统一的成功/失败信封。
 * <p>
 * 控制器会把它渲染成 {@code {success, result, error}} 的 JSON 回给 Python 侧。
 */
public final class ToolResult {

    private final boolean success;
    private final String result;
    private final String error;

    private ToolResult(boolean success, String result, String error) {
        this.success = success;
        this.result = result;
        this.error = error;
    }

    public static ToolResult ok(String result) {
        return new ToolResult(true, result, null);
    }

    public static ToolResult error(String error) {
        return new ToolResult(false, null, error);
    }

    public boolean isSuccess() {
        return success;
    }

    public String getResult() {
        return result;
    }

    public String getError() {
        return error;
    }
}

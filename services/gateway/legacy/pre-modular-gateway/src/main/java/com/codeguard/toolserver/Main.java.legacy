package com.codeguard.toolserver;

/**
 * 工具服务进程入口:{@code java -jar codeguard-gateway.jar}。
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        ToolServerApp app = new ToolServerApp();
        app.start(app.port());
        Runtime.getRuntime().addShutdownHook(new Thread(app::stop));
    }
}

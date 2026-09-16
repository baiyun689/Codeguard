package com.codeguard;

import com.codeguard.ci.CiServerApp;
import com.codeguard.proxy.ProxyServer;
import com.codeguard.toolserver.ToolServerApp;

/** 启动三个独立的 Spring Boot 服务上下文，将内部接口与公共端口隔离。 */
public final class Main {
    private Main() {}
    public static void main(String[] args) {
        ToolServerApp tools = new ToolServerApp();
        CiServerApp ci = new CiServerApp();
        ProxyServer proxy = new ProxyServer();
        try {
            tools.start(tools.port());
            ci.start();
            proxy.start();
        } catch (RuntimeException | Error failure) {
            proxy.stop();
            ci.stop();
            tools.stop();
            throw failure;
        }
        // 每个服务上下文注册独立的关闭钩子，并按依赖顺序销毁组件。
    }
}

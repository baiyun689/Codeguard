package com.codeguard;

import com.codeguard.ci.CiServerApp;
import com.codeguard.proxy.ProxyServer;
import com.codeguard.toolserver.ToolServerApp;

/** Launch three isolated Spring Boot contexts without exposing internal routes on the public port. */
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
        // Each Boot context registers its own shutdown hook and destroys dependent beans in order.
    }
}

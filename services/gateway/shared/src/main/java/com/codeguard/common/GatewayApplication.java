package com.codeguard.common;

import org.springframework.boot.Banner;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;

import java.util.Map;

/** 启动独立的 Spring Boot Servlet 上下文，每个端口仅注册所属服务的控制器。 */
public final class GatewayApplication implements AutoCloseable {
    private final ConfigurableApplicationContext context;

    private GatewayApplication(ConfigurableApplicationContext context) {
        this.context = context;
    }

    public static GatewayApplication start(Class<?> configuration, int port, Map<String, Object> inputs) {
        var context = new SpringApplicationBuilder(configuration)
                .bannerMode(Banner.Mode.OFF)
                .initializers(application -> inputs.forEach(
                        (name, bean) -> application.getBeanFactory().registerSingleton(name, bean)))
                .run("--server.port=" + port,
                        "--spring.application.name=" + configuration.getSimpleName(),
                        "--server.shutdown=graceful",
                        "--spring.lifecycle.timeout-per-shutdown-phase=30s",
                        "--spring.jmx.enabled=false");
        return new GatewayApplication(context);
    }

    public int port() {
        return ((ServletWebServerApplicationContext) context).getWebServer().getPort();
    }

    public ConfigurableApplicationContext context() { return context; }

    @Override
    public void close() { context.close(); }
}

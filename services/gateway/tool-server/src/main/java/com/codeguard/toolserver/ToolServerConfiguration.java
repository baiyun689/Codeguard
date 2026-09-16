package com.codeguard.toolserver;

import com.codeguard.common.GatewayHttpConfiguration;
import com.codeguard.common.GatewayMetrics;
import com.codeguard.common.OperationalController;
import com.codeguard.agent.graph.ProjectSnapshotManager;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.web.filter.OncePerRequestFilter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

@SpringBootConfiguration(proxyBeanMethods = false)
@EnableAutoConfiguration(exclude = DataSourceAutoConfiguration.class)
@Import(GatewayHttpConfiguration.class)
public class ToolServerConfiguration {
    @Bean GatewayMetrics gatewayMetrics() { return new GatewayMetrics(); }
    @Bean ProjectSnapshotManager projectSnapshotManager(GatewaySettings settings) {
        return new ProjectSnapshotManager(settings.graphCacheMaxSnapshots(),
                settings.graphCacheTtl(), settings.graphBuildTimeout());
    }
    @Bean WorkspaceAccessPolicy workspaceAccessPolicy(GatewaySettings settings) {
        return new WorkspaceAccessPolicy(settings.toolAllowedRoots());
    }
    @Bean ToolSessionManager toolSessionManager(ProjectSnapshotManager snapshots, WorkspaceAccessPolicy policy) {
        return new ToolSessionManager(snapshots, policy);
    }
    @Bean ToolServerController toolController(GatewayMetrics metrics, ToolSessionManager sessions, ObjectMapper mapper) {
        return new ToolServerController(metrics, sessions, mapper);
    }
    @Bean OperationalController operationalController(GatewayMetrics metrics) {
        return new OperationalController(() -> true, metrics);
    }
    @Bean FilterRegistrationBean<OncePerRequestFilter> toolAuthentication(GatewaySettings settings) {
        var filter = new FilterRegistrationBean<OncePerRequestFilter>();
        filter.setOrder(org.springframework.core.Ordered.HIGHEST_PRECEDENCE + 10);
        filter.addUrlPatterns("/api/v1/tools/*");
        filter.setFilter(new OncePerRequestFilter() {
            @Override protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                    FilterChain chain) throws ServletException, IOException {
                String supplied = request.getHeader("X-Codeguard-Tool-Token");
                if (supplied == null || !MessageDigest.isEqual(
                        settings.toolServerToken().getBytes(StandardCharsets.UTF_8),
                        supplied.getBytes(StandardCharsets.UTF_8))) {
                    response.setStatus(401);
                    response.setContentType("application/json");
                    response.getWriter().write("{\"success\":false,\"error\":\"unauthorized\"}");
                    return;
                }
                chain.doFilter(request, response);
            }
        });
        return filter;
    }
}

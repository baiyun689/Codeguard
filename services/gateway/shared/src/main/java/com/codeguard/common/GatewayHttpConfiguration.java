package com.codeguard.common;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.ByteArrayInputStream;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** Preserve the old 10 MB HTTP body bound, including chunked requests. */
@Configuration(proxyBeanMethods = false)
public class GatewayHttpConfiguration {
    public static final int MAX_REQUEST_BYTES = 10_000_000;

    @Bean
    FilterRegistrationBean<OncePerRequestFilter> requestSizeLimit() {
        var registration = new FilterRegistrationBean<OncePerRequestFilter>();
        registration.setOrder(org.springframework.core.Ordered.HIGHEST_PRECEDENCE + 20);
        registration.setFilter(new OncePerRequestFilter() {
            @Override
            protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                            FilterChain chain) throws ServletException, IOException {
                if (request.getContentLengthLong() > MAX_REQUEST_BYTES) {
                    response.sendError(413);
                    return;
                }
                byte[] body = request.getInputStream().readNBytes(MAX_REQUEST_BYTES + 1);
                if (body.length > MAX_REQUEST_BYTES) {
                    response.sendError(413);
                    return;
                }
                chain.doFilter(new BufferedRequest(request, body), response);
            }
        });
        return registration;
    }

    private static final class BufferedRequest extends HttpServletRequestWrapper {
        private final byte[] body;
        BufferedRequest(HttpServletRequest request, byte[] body) {
            super(request);
            this.body = body;
        }
        @Override
        public ServletInputStream getInputStream() {
            var input = new ByteArrayInputStream(body);
            return new ServletInputStream() {
                @Override public int read() { return input.read(); }
                @Override public int read(byte[] b, int off, int len) { return input.read(b, off, len); }
                @Override public boolean isFinished() { return input.available() == 0; }
                @Override public boolean isReady() { return true; }
                @Override public void setReadListener(ReadListener listener) {
                    throw new UnsupportedOperationException("Synchronous request bodies only");
                }
            };
        }
        @Override public BufferedReader getReader() {
            return new BufferedReader(new InputStreamReader(getInputStream(), StandardCharsets.UTF_8));
        }
    }
}

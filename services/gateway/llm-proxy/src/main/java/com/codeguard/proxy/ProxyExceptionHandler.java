package com.codeguard.proxy;

import com.codeguard.proxy.handler.ChatCompletionsHandler;
import com.codeguard.proxy.model.OpenAiChatResponse;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@RestControllerAdvice(assignableTypes = ChatCompletionsHandler.class)
public class ProxyExceptionHandler {
    private static final Logger log = LoggerFactory.getLogger(ProxyExceptionHandler.class);
    @ExceptionHandler(Exception.class)
    public ResponseEntity<OpenAiChatResponse.ErrorResponse> unexpected(Exception error) {
        log.error("LLM Proxy unexpected error", error);
        return ResponseEntity.status(500).body(OpenAiChatResponse.error(
                "Internal proxy error", "proxy_error", "500"));
    }
}

package com.tradeflow.application.port;

import java.net.URI;
import java.time.Duration;
import java.util.Map;

public interface ExternalHttpClient {
    String get(URI uri, Duration timeout);
    String post(URI uri, Map<String, Object> body, Duration timeout, String requestId);
}

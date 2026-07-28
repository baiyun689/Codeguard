package com.tradeflow.application.port;

import java.io.InputStream;
import java.nio.file.Path;

public interface FileStore {
    Path exportRoot(String tenantId);
    InputStream open(Path path);
    void write(Path path, InputStream content);
}

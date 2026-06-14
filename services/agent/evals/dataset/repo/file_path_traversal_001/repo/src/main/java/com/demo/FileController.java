package com.demo;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;

public class FileController {

    private static final String BASE = "/var/data";

    public byte[] download(String name) throws IOException {
        File f = resolve(name);
        return Files.readAllBytes(f.toPath());
    }

    private File resolve(String name) {
        return new File(BASE + "/" + name);
    }
}

package com.demo;

public class InvoiceController {

    private final RenderEngine engine;

    public InvoiceController(RenderEngine engine) {
        this.engine = engine;
    }

    public String headerOf(String id) {
        return engine.render(id).trim();
    }
}

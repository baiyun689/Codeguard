package com.demo;

public class AccountController {

    private final AccountService service;

    public AccountController(AccountService service) {
        this.service = service;
    }

    public String labelOf(String id) {
        return service.nameOf(id).trim();
    }
}

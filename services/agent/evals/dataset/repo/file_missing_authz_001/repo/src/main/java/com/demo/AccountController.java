package com.demo;

public class AccountController {

    private final long[] balances = new long[100];

    public String transfer(int from, int to, long amount) {
        doTransfer(from, to, amount);
        return "ok";
    }

    private void doTransfer(int from, int to, long amount) {
        balances[from] -= amount;
        balances[to] += amount;
    }
}

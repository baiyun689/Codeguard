package com.demo.notify;

public class NotificationSender {

    public void send(String to, String message) {
        if (to == null || message == null) {
            return;
        }
        System.out.println("[notify] " + to + ": " + message);
    }
}

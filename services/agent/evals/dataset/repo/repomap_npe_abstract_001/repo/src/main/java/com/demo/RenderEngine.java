package com.demo;

/**
 * 渲染引擎基类。render 是抽象方法——空安全性由具体子类决定,
 * 光看这个基类看不出 render 会不会返回 null。
 */
public abstract class RenderEngine {

    public abstract String render(String id);
}

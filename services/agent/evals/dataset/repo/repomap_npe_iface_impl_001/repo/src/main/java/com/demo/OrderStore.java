package com.demo;

/**
 * 订单编码读取入口。注意:这里只有方法声明,看不出 lookup 的空安全性——
 * 找不到时是返回 null 还是抛异常,取决于具体实现(在另一个文件里)。
 */
public interface OrderStore {

    String lookup(String id);
}

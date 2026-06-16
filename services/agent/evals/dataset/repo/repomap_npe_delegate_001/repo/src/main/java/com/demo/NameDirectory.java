package com.demo;

/**
 * 名称目录。resolve 找不到时返回什么(null 还是抛异常)取决于实现——
 * 这里只有声明,看不出空安全性。
 */
public interface NameDirectory {

    String resolve(String id);
}

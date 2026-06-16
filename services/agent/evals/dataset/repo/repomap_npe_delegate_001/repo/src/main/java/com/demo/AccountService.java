package com.demo;

/**
 * 账户服务:把名称查询委托给目录组件。注意这里不是 null 的来源——
 * nameOf 只是把结果原样转发,真正的数据源在 NameDirectory 的某个实现里。
 */
public class AccountService {

    private final NameDirectory directory;

    public AccountService(NameDirectory directory) {
        this.directory = directory;
    }

    public String nameOf(String id) {
        return directory.resolve(id);
    }
}

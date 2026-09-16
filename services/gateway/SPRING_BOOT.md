# Spring Boot Gateway

Java Gateway 使用 Spring Boot 3.5.16、Spring MVC、Tomcat 和 Java 21，已移除 Javalin 及其测试依赖。业务仍分为 shared、tool-server、ci-webhook、llm-proxy 四个 Maven 模块。

## 应用结构

```text
Main（统一进程入口；任一服务启动失败时关闭已启动服务）
 ├─ ToolServerApp → ToolServerConfiguration → Boot 上下文 / 9090
 │   ├─ ToolServerController：会话创建、删除、工具分发
 │   ├─ ProjectSnapshotManager / ToolSessionManager / WorkspaceAccessPolicy
 │   └─ Token Filter → 请求大小 Filter → Spring MVC
 ├─ CiServerApp → CiServerConfiguration → Boot 上下文 / 8080
 │   ├─ GitHubWebhookController：保留原始字节验签
 │   ├─ JobRepository / ReviewExecutorImpl / JobScheduler / ResultFeedback
 │   └─ 有 webhook secret 时启用业务 Bean；未配置时仅提供运维端点
 └─ ProxyServer → ProxyConfiguration → Boot 上下文 / 9091
     ├─ ChatCompletionsHandler：OpenAI 兼容协议与降级链
     ├─ ProviderRouter / ResilienceService / Provider adapters
     └─ ProxyExceptionHandler：模型协议的统一异常响应

每个上下文独立拥有 OperationalController、GatewayMetrics 和适用的 AlertEvaluator。
```

三个配置类使用 `@SpringBootConfiguration`、`@EnableAutoConfiguration`、`@Import` 和 `@Bean`。共同的启动代码位于 `shared/.../GatewayApplication.java`，通过 SpringApplicationBuilder 创建独立的 Servlet Web 应用。没有对整个 `com.codeguard` 包做组件扫描。

保留三个上下文是为了兼容端口并隔离路由：公共 Webhook 端口不提供内部源码工具，也不提供 LLM 代理接口。它们仍在一个 JVM 中，不代表三套独立部署的微服务，也不提供进程级故障隔离。部署时继续只公开所需端口。

## Spring 管理的内容

- **MVC 接口：** `@RestController`、`@PostMapping`、`@DeleteMapping`、`@GetMapping`、`ResponseEntity`；控制器通过构造器接收依赖。
- **依赖组装：** 配置类使用 `@Bean` 组装工具会话、索引缓存、调度器、Repository、模型路由和指标组件；不在 Controller 中启动服务器。
- **生命周期：** Scheduler 和 AlertEvaluator 使用 `initMethod="start" / destroyMethod="close"`。Repository 在依赖它的 Scheduler 关闭后由 Spring 关闭；Scheduler 不再关闭注入的 Repository。
- **条件配置：** CI 根据已解析的 webhook secret 决定是否创建数据库和后台调度组件，未启用 CI 时不要求 MySQL 在线。
- **自动配置：** Boot 提供 MVC、Tomcat、JSON 和日志等基础设施。显式排除 DataSourceAutoConfiguration，保留既有 JDBC/HikariCP 配置与数据库合同。
- **构建：** Boot parent 管理常用依赖版本；`spring-boot-maven-plugin` 将 CI 主模块重新打包为可执行 JAR，其他模块作为普通依赖放在 `BOOT-INF/lib`。

这次没有引入 JPA、Spring Security、`@Transactional` 或 `@Async`；线程池调度和 Resilience4j 仍使用现有实现。不能因为迁移到 Boot 就声称具备上述未实现能力。

## 保持的合同

| 服务 | 接口 / 行为 |
|---|---|
| 工具 | `/api/v1/tools/session`、`/api/v1/tools/{name}`，原 token/header 与 JSON 信封 |
| Webhook | `/webhooks/github`，原始 byte[] HMAC、状态码、幂等与后台调度 |
| 模型代理 | `/v1/chat/completions`，模型映射、结构化响应及失败降级 |
| 运维 | `/health`、`/health/live`、`/health/ready`、`/metrics`，适用服务的 `/health/slo` |
| 大小限制 | HTTP body 上限 10,000,000 字节，包含 chunked 传输；工具认证先于 body 读取 |
| 配置 | 保留 GatewaySettings / ProxyConfig 及现有 CODEGUARD 环境变量；未伪装成全部已迁移 ConfigurationProperties |
| Python | Agent 的协议、提示词、预算、节点和质量评测不变 |

`GatewayHttpConfiguration` 的 Filter 在 MVC 读取请求体之前限制大小，保留原始字节供 webhook 验签。工具 Token Filter 的顺序更早。请求体是同步有界读取，不支持异步 Servlet body 消费。

## 构建与运行

Windows PowerShell，在 `services/gateway` 下：

```powershell
.\mvnw.cmd --batch-mode verify
java -jar ci-webhook/target/codeguard-gateway.jar
```

Linux / macOS：

```bash
sh ./mvnw --batch-mode verify
java -jar ci-webhook/target/codeguard-gateway.jar
```

Wrapper 固定 Maven 3.9.9，并校验下载包 SHA-256。首次需要下载；JDK 仍由本机提供，要求 21。本机旧 Maven 3.6.1 无法执行当前插件，请用 Wrapper。Docker 构建环境本来就是 Maven 3.9.9 / JDK 21，不需要变更服务地址。

Spring Boot JAR 使用嵌套依赖，启动方式为 `java -jar`。不要再把可执行 JAR 当成平铺 classpath 直接 `java -cp ... com.codeguard.Main`；自行编写的本地辅助脚本如使用旧 shade 布局，需要相应调整。

## 验证与边界

迁移时通过了 Java 全模块 `verify` 和新增 HTTP/生命周期合同验证，并从最终 Boot JAR 启动三个端口进行冒烟检查。测试包括：

当前测试报告共 100 项：98 项通过，2 项需要符号链接能力的既有测试在 Windows 环境跳过，失败与错误均为 0。完整验证后对请求 Filter 顺序和大小上限新增检查，再运行相关 HTTP 合同测试通过。

- 工具认证、允许目录、真实符号读取、拒绝旧文件路径入参。
- chunked 超大请求返回 413；认证失败优先返回 401。
- Webhook 原始体签名、事件分流、有效 PR 异步接受。
- 未启用 CI 时无需数据库；启用 CI 时 Bean 正确装配，Python 不可用时 readiness 返回 503。
- Spring 关闭上下文时停止 Scheduler；外部注入的 Repository 不被 Scheduler 越权关闭。
- 本地模拟 Provider 验证代理模型映射、响应 JSON、非法输入不触发上游调用。
- CI 和 Proxy 端口不能访问工具接口；空路由代理 readiness 返回 503。

没有运行付费模型评测，也没有验证实际 GitHub 回写、生产 MySQL 或 Docker 部署。历史质量指标属于迁移前的运行记录，不能当作本次迁移的效果结论。现有单实例调度、无界任务队列、反馈补偿缺口和模型协议问题，不会因切换框架自动消失。

## 面试口述

> 项目原来用轻量 HTTP 框架，后来迁移到 Spring Boot。我没有只加启动注解，而是把接口改为 Spring MVC，把调度、工具和代理依赖交给 Bean 管理，也处理了关闭顺序。为了兼容原有端口并保护内部工具，我保留三个独立应用上下文，不做全包扫描。迁移重点是 HTTP 合同和生命周期，已有模型审查流程没有改，也没有把框架迁移包装成 Recall 提升。

容易引出的追问：为什么配置类禁用代理？没有组件扫描怎样注册 Controller？依赖销毁顺序怎样决定？为什么排除数据源自动配置？原始 byte[] 与 JSON 绑定对验签有什么影响？Spring 容器关闭与强制杀进程有何区别？这些问题可以从当前配置和测试中定位答案。

# 🔍 Codeguard 审查报告

> **仓库** `/workspace/projects/springboot-review-demo` · **基准** `6674e93a5e1cd68d2738c09a17a0e151adafde11` · **模型** `deepseek-v4-flash` · **耗时** `90s`

---

## 📊 总览

| 🔴 CRITICAL | 🟡 WARNING | 🔵 INFO |
| :---: | :---: | :---: |
| **1** | **12** | **4** |

## 🔴 CRITICAL

### 1. 硬编码凭据（CONFIG_SECURITY） · `src/main/java/com/example/demo/NotificationService.java:18`

**问题**:API_KEY 以明文硬编码在源码常量中，并随每次出站 webhook 请求以 Authorization: Bearer 头发送（C03、C04）。该值会进入：1) 以明文编译进 class 文件与仓库历史；2) 若 targetUrl 指向攻击者主机则直接泄露完整密钥。即使远端不可控，凡能读取仓库/制品的人均可获得该凭据。根因是生产凭据被内置在代码中而非外部化（环境变量/机密管理）。

**建议**:将 API_KEY 从源码常量移出，改为通过环境变量或配置/密钥管理服务注入（如 System.getenv("NOTIFY_API_KEY")），并轮换该已泄露的密钥。

```java
...
@Service
public class NotificationService {

    private static final Logger log = LoggerFactory.getLogger(NotificationService.class);
    private static final String API_KEY = "sk-ntf-8a7b3c2d1e4f5a6b7c8d9e0f";  // ← 第18行

    public String sendWebhook(String targetUrl, String payload) throws Exception {
        URL url = new URL(targetUrl);
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
...
```

> 🎯 置信度 **0.95**

## 🟡 WARNING

### 2. RESOURCE_LIFECYCLE · `src/main/java/com/example/demo/DBHelper.java:8`

**问题**:getConnection 返回的 Connection 在调用链中从未被关闭。经 inspect_change_impact 确认，OrderService.processOrder（OrderService.java:20）调用 DBHelper.getConnection 获取连接的代码行，但该 Connection 在 processOrder 中只被赋值给局部变量 conn，后续既无 try-with-resources 也无 finally 关闭（T02 可见 processOrder 在获取后无任何关闭逻辑）。调用 getConnection 的每次调用都会创建一个新的 JDBC 连接，长时间或高频调用下会导致数据库连接耗尽，DB 服务退化或不可用。这是本 task 新增的连接获取方法被调用方以不关闭的方式使用所暴露的资源生命周期缺陷。根因在于 DBHelper.getConnection 向调用方交付了必须由调用方负责关闭的生命周期责任，而当前唯一调用方没有履行该责任；本 task 未提供任何复用池机制来兜底（方法直接用 DriverManager.getConnection 新建连接）。

**建议**:在调用方 OrderService.processOrder 中用 try-with-resources 包裹 Connection（如 try (Connection conn = DBHelper.getConnection(dbPassword)) {...}），确保连接在所有路径上关闭。或在 DBHelper 内改为使用连接池（如 HikariCP DataSource 单例）而非每次新建裸连接。

```java
...
import java.sql.DriverManager;

public class DBHelper {
    public static Connection getConnection(String password) {
        try {  // ← 第8行
            return DriverManager.getConnection("jdbc:h2:mem:testdb", "sa", password);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
...
```

> 🎯 置信度 **0.80**

### 3. RESOURCE_LIFECYCLE · `src/main/java/com/example/demo/NotificationService.java:21`

**问题**:HttpURLConnection 在 sendWebhook 中打开后从未关闭。方法在错误路径会抛出 RuntimeException（code 非 2xx 时）或传播 IOException，而 conn 没有 finally 块关闭，连接对象持有底层 Socket/输入输出流，会一直保持打开直到 GC。在多次调用 sendWebhook（每个 notifyUser 都可能触发）的情况下，会导致文件描述符/连接耗尽，造成后续 webhook 调用的时序性失败。sendWebhook 无外部调用者（T01 显示 notifyUser 也未在 MAIN 中被调用，coverage=complete），但该方法本身是 public 且带 @Service，未来被调用即触发该泄漏。连接资源应通过 try-with-resources 或 finally 释放（disconnect），错误路径也不应泄漏。

**建议**:在 sendWebhook 开头保存 conn，并在 finally 块中调用 conn.disconnect()，或用 try-with-resources 包裹连接生命周期，确保正常和异常路径都释放底层连接。

```java
...
    private static final Logger log = LoggerFactory.getLogger(NotificationService.class);
    private static final String API_KEY = "sk-ntf-8a7b3c2d1e4f5a6b7c8d9e0f";

    public String sendWebhook(String targetUrl, String payload) throws Exception {
        URL url = new URL(targetUrl);  // ← 第21行
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setRequestMethod("POST");
        conn.setRequestProperty("Authorization", "Bearer " + API_KEY);
        conn.setRequestProperty("Content-Type", "application/json");
...
```

> 🎯 置信度 **0.80**

### 4. INPUT_VALIDATION · `src/main/java/com/example/demo/NotificationService.java:45`

**问题**:notifyUser 将调用方传入的 message 直接拼接到 JSON payload 字符串，没有进行转义。若 message 含双引号、反斜杠、换行或控制字符，产生的 payload 将是无效 JSON，远端 webhook 解析失败；message 含 \" 或可注入额外字段，会破坏 JSON 结构（payload 注入），导致通知内容错误或伪造字段。review_plan 也点名了这一检查方向。

**建议**:使用 JSON 序列化库（如 ObjectMapper）构造 payload，或将 message 中的特殊字符（\"、\\、\n 等）正确转义后再拼接，保证 payload 始终为合法 JSON。

```java
...
        if (endpoint == null) {
            endpoint = "http://localhost:8089/notify";
        }
        try {
            sendWebhook(endpoint + "/users/" + userId, "{\"message\":\"" + message + "\"}");  // ← 第45行
        } catch (Exception e) {
            log.warn("Notification failed: {}", e.getMessage());
        }
    }
...
```

> 🎯 置信度 **0.75**

### 5. integer_overflow_in_arithmetic · `src/main/java/com/example/demo/ScoreCalculator.java:71`

**问题**:baseScore 和 count 均为 int，乘法 baseScore * count 在赋值给 long rawTotal 之前以 32 位 int 完成运算，可能发生整数溢出。baseScore 语义为 0-100，count 为批量数量；当 count 较大（如 count > 2100 万，或任一侧不能压缩边界）时乘积超过 Integer.MAX_VALUE 会溢出为负数或回绕，导致（a）rawTotal 判断 rawTotal > 10000 时不成立，(b) 返回的 rawTotal 为错误值，批量折扣结果错误。注释声明 baseScore 为 0-100，count 无上限声明，因此触发路径可信。修复需先转换一侧为 long：long rawTotal = (long) baseScore * count。

**建议**:将乘法改为 (long) baseScore * count，确保在 long 语义下计算避免 int 溢出，同时保持返回类型 long 的语义一致。

```java
...

    /** 计算批量折扣后的总分。baseScore 单条分数 0-100，count 数量。 */
    public long applyBatchDiscount(int baseScore, int count) {
        long rawTotal = baseScore * count;
        if (rawTotal > 10000) {  // ← 第71行
            return (long) (rawTotal * 0.85);
        }
        return rawTotal;
    }
...
```

> 🎯 置信度 **0.75**

### 6. TOKEN_FORMAT_CONTRACT_MISMATCH · `src/main/java/com/example/demo/TokenService.java:26`

**问题**:generateToken 生成格式为 userId + "-" + 32位随机串，而 validateToken 用 token.split("-") 并严格要求 parts.length==2。若调用方传入的 userId 本身包含连字符（如 'u-123'），generateToken 会生成 'u-123-xxxxxxxx...'，split 结果有 3 段，validateToken 直接返回 false，导致本应合法的 token 被判定无效。此外 validateToken 只校验 parts[1].length()==32，未校验 parts[1] 是否确实由字母数字组成，任何含 '-' 且第二段长度为 32 的任意字符串都会被接受为合法 token（弱校验，无法区分伪造 token）。generateToken 与 validateToken 之间格式契约不一致是本 task 引入的缺陷。触发条件：userId 含连字符时合法 token 校验失败（false negative）。可观察后果：合法用户 token 被拒绝，功能失效。由于 T01/T02 显示 MAIN 范围内无调用方（complete），实际影响范围取决于未来调用，但契约不一致本身在当前文件内即成立。

**建议**:使校验与生成格式完全对齐：generateToken 与 validateToken 应对 token 的段结构和 userId 字符集达成一致。可选方案：1) generateToken 要求/归一化 userId 不含 '-'，或 2) validateToken 按最后一个 '-' 分隔（split("-", -1) 取末段作为随机串）并校验该段长度与字符集，且额外校验 parts[0] 与期望 userId 一致。若仅需弱校验，应至少校验第二段仅含 [A-Za-z0-9]。

```java
...
    }

    public boolean validateToken(String token) {
        try {
            String[] parts = token.split("-");  // ← 第26行
            if (parts.length != 2) {
                return false;
            }
            return parts[1].length() == 32;
...
```

> 🎯 置信度 **0.75**

### 7. RESOURCE_LIFECYCLE / 资源生命周期：FileInputStream 未关闭存在句柄泄漏 · `src/main/java/com/example/demo/XmlReportParser.java:39`

**问题**:readFirstSection 中 FileInputStream fis 通过 new FileInputStream(filePath) 创建，但既没有用 try-with-resources，也没有在任何路径（包括成功返回后）关闭它。每次调用都会泄漏一个 OS 文件句柄。在持续调用该 Service 方法的场景下（例如每次上传报表被解析），文件句柄持续累积会导致 open file limit 耗尽，最终使文件打开/上传操作抛出 IOException，核心报表解析功能退化不可用。修复位置在方法体：需要以异常安全的方式关闭 fis。；readFirstSection 直接创建 `FileInputStream fis` 并在读取字节后返回，全程未调用 close()，也没有 try-with-resources。FileInputStream 依赖底层文件描述符，若该 Service 在长生命周期应用（尤其被用于循环解析多个文件时）中多次调用，会持续累积打开的文件句柄直到 GC 触发 finalizer 才释放，在 Windows/缺 fd 环境下最终耗尽句柄。相比 parseReport 与 extractFields 中 FileReader 同样未关闭，但 parseReport/extractFields 通过 builder.parse(InputSource) 传入的流由解析器读取。readFirstSection 是最直接的裸流泄漏。建议改为 try-with-resources 包裹 fis 或使用 Files.read... 方式。

**建议**:将 readFirstSection 改为 try-with-resources 形式：try (FileInputStream fis = new FileInputStream(filePath)) { ... }，确保成功与异常路径都释放文件句柄。；将 readFirstSection 改为 try (FileInputStream fis = new FileInputStream(filePath)) { ... }，确保所有 return 路径都关闭流；也可以用 Files.readAllBytes 替代手动流管理。

```java
...
        return records;
    }

    public String readFirstSection(String filePath) throws Exception {
        FileInputStream fis = new FileInputStream(filePath);  // ← 第39行
        byte[] buffer = new byte[1024];
        int bytesRead = fis.read(buffer);
        if (bytesRead <= 0) {
            return "";
...
```

> 🎯 置信度 **0.85**

### 8. RESOURCE_LIFECYCLE · `src/main/java/com/example/demo/XmlReportParser.java:25`

**问题**:parseReport 方法以 new FileReader(filePath) 作为 builder.parse 的输入源。FileReader 使用平台默认字符集解码，非 UTF-8 环境下解析 UTF-8 XML 或含非 ASCII 字符的报表时会产生乱码（java.io.FileReader 的编码无法指定，且平台默认编码不可控）。同时 DocumentBuilderFactory.newInstance() 创建的 factory 未调用 setFeature 禁用外部实体（XXE 防护缺失），解析含外部实体/DTD 的恶意 XML 时存在 XXE 风险。这些属于跨环境行为缺陷。

**建议**:改用显式编码的 InputSource：new InputSource(new InputStreamReader(new FileInputStream(filePath), StandardCharsets.UTF_8))，并在 factory 上设置禁用外部实体/DTD 的特性（FEATURE_SECURE_PROCESSING、disallow-doctype-decl 等）。

```java
...
        Document doc = builder.parse(new InputSource(new FileReader(filePath)));
        NodeList items = doc.getElementsByTagName("item");
        for (int i = 0; i < items.getLength(); i++) {
            Element item = (Element) items.item(i);
            Map<String, String> record = new HashMap<>();  // ← 第25行
            NodeList children = item.getChildNodes();
            for (int j = 0; j < children.getLength(); j++) {
                Node child = children.item(j);
                if (child.getNodeType() == Node.ELEMENT_NODE) {
...
```

> 🎯 置信度 **0.70**

### 9. XML 解析未配置安全特性（XXE 风险） · `src/main/java/com/example/demo/XmlReportParser.java:20`

**问题**:parseReport 与 extractFields 都通过 `DocumentBuilderFactory.newInstance()` 直接创建解析器并解析外部 XML 文件。默认的 DocumentBuilderFactory 未禁用外部实体/DTD 装载，对不可信 XML 输入存在 XXE（外部实体注入）与实体膨胀攻击风险。由于该 @Service 设计为通用 XML 报告解析入口，未来很可能被用于解析用户上传或外部来源的文件，届时攻击者可控的 XML 可直接导致本地文件读取或资源耗尽。维护者修改该方法时不一定意识到需要手动加固 factory，形成持续的安全债务。建议统一收敛到一个创建安全 factory 的逻辑（显式 setFeature 禁用 DOCTYPE/external entities），并复用该逻辑。

**建议**:提取一个私有方法返回已禁用外部实体与 DTD 的 DocumentBuilderFactory（设置 XMLConstants.FEATURE_SECURE_PROCESSING、DisallowDocTypeDecl、external-general-entities 与 external-parameter-entities 为 false），parseReport 与 extractFields 共用它，消除重复构造并统一加固。

```java
...

    public List<Map<String, String>> parseReport(String filePath) throws Exception {
        List<Map<String, String>> records = new ArrayList<>();
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        DocumentBuilder builder = factory.newDocumentBuilder();  // ← 第20行
        Document doc = builder.parse(new InputSource(new FileReader(filePath)));
        NodeList items = doc.getElementsByTagName("item");
        for (int i = 0; i < items.getLength(); i++) {
            Element item = (Element) items.item(i);
...
```

> 🎯 置信度 **0.90**

### 10. 敏感信息泄露（日志） · `src/main/java/com/example/demo/NotificationService.java:34`

**问题**:失败路径将 API_KEY 前 8 位通过 log.error 输出到日志（C03、C04）。虽然只输出前 8 位，仍部分暴露了凭据空间，且该 errorMsg 同时还被包进 RuntimeException 抛出；若该异常沿调用链返回客户端响应，则凭据前缀会越过信任边界。日志系统与异常处理器均未做脱敏。

**建议**:从日志与异常消息中移除 API_KEY 相关内容，改为仅记录 webhook 状态码与去敏后的请求标识；集中异常处理时避免将内部 errorMsg 反射给客户端。

```java
...
        int code = conn.getResponseCode();
        if (code >= 200 && code < 300) {
            return "OK";
        }
        String errorMsg = "Webhook failed with code " + code + ", API_KEY=" + API_KEY.substring(0, 8) + "...";  // ← 第34行
        log.error(errorMsg);
        throw new RuntimeException(errorMsg);
    }

...
```

> 🎯 置信度 **0.80**

### 11. WEAK_TOKEN_PREDICTABILITY · `src/main/java/com/example/demo/TokenService.java:39`

**问题**:createPasswordResetToken 生成的密码重置令牌仅由三部分组成：email.hashCode()（Java String 哈希，确定且易被暴力枚举）、System.currentTimeMillis() 毫秒时间戳（可被攻击者观测或估计）、以及随机 0~999999 的整数（仅 100 万种组合）。由于使用非加密安全的 java.util.Random（C02），且随机空间只有 10^6，加上时间戳和 hashCode 均可预测，攻击者可在短时间内枚举出有效密码重置令牌，进而接管任意用户账号（重置其密码）。validateToken 只校验按 '-' 分割后长度==32 的部分，对 reset token 不适用也无法提供真实性防护（C04）。本问题由当前新增的 createPasswordResetToken 方法直接引入。图谱查询该方法的调用路径返回 not_found+complete（T01），说明当前尚无已解析生产调用方，完整利用链依赖后续接线，故置信度下调至 0.6。建议：改用 SecureRandom（或随机 UUID v4）生成至少 128 位真随机令牌，并配合服务端持久化存储 + 过期时间 + 单次使用校验，不要依赖可预测的 hashCode 和时间戳。

**建议**:使用 java.security.SecureRandom 生成至少 32 字节随机令牌，服务端存储令牌摘要并绑定 email、设置过期时间，校验时比对存储值且使用后立即作废。禁止仅用 hashCode+时间戳+100 万种随机数的组合作为身份凭据。

```java
...

    public String createPasswordResetToken(String email) {
        long timestamp = System.currentTimeMillis();
        int randomPart = random.nextInt(1000000);
        return email.hashCode() + "-" + timestamp + "-" + randomPart;  // ← 第39行
    }
}
```

> 🎯 置信度 **0.60**

### 12. INSECURE_RANDOM_FOR_TOKEN · `src/main/java/com/example/demo/TokenService.java:16`

**问题**:generateToken 使用 java.util.Random（C02）生成 32 字符会话令牌。java.util.Random 是线性同余伪随机数生成器，其输出可预测——一旦攻击者观测到部分 token 或获知种子相关信息，即可推算出后续 token。对于会话/认证令牌这类安全敏感凭据，必须使用加密安全随机源（SecureRandom）。此外 validateToken 仅验证按 '-' 拆分后第二部分长度为 32（C04），无法验证令牌真实性——任意攻击者可手工构造 '任意userID-'+32 个任意字符的字符串被接受，因为 validate 不核查该随机串是否由系统签发或对应有效会话。图谱查询 generateToken/validateToken 调用路径均返回 not_found+complete（T02/T03），当前无生产接线，故评为 WARNING。建议：改用 SecureRandom 生成令牌，并在 validateToken 中通过服务端签发记录（如数据库/缓存中的哈希或会话绑定）核验令牌真实性及过期。

**建议**:将 Random 替换为 SecureRandom；validateToken 不应只靠字符串长度判断合法，应查询服务端存储的已签发令牌（含哈希、过期与用户绑定）做真实性校验。

```java
...

    private final Random random = new Random();

    public String generateToken(String userId) {
        StringBuilder sb = new StringBuilder();  // ← 第16行
        String chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        for (int i = 0; i < 32; i++) {
            sb.append(chars.charAt(random.nextInt(chars.length())));
        }
...
```

> 🎯 置信度 **0.70**

### 13. PATH_TRAVERSAL · `src/main/java/com/example/demo/UserController.java:113`

**问题**:downloadReport 端点：filename 参数直接拼接为 "reports/" + filename 得到路径，未做 normalize 与 startsWith 边界校验，Files.readAllBytes(path) 将该路径整个返回给客户端。攻击者可传入 ../../../../etc/passwd 等路径读取服务器上任意权限可读的文件。需说明：该段代码在本次 diff 中相对于删除侧内容逐字相同，无行为变更，属于既有缺陷；但由于本 task 的定位范围以 new-side 行呈现，仍按文件内问题报告。此外整个 UserController 无任何认证/授权注解，这些文件读写端点均可被匿名访问。触发条件：攻击者直接 GET /users/download-report?filename=../../../../etc/passwd（filename 无需校验）。后果：任意文件读取直至应用进程可访问范围。

**建议**:downloadReport 使用与 export/import 相同的防御：exportDir.resolve(filename).normalize() 后校验 startsWith(exportDir) 且仅允许文件名（拒绝绝对路径、../、反斜杠）；并为所有用户数据访问端点增加鉴权（如 @PreAuthorize 或认证主体绑定）。

```java
...
    public ResponseEntity<String> downloadReport(@RequestParam String filename) throws IOException {
        String reportPath = "reports/" + filename;
        Path path = Paths.get(reportPath);
        String content = new String(Files.readAllBytes(path));
        return ResponseEntity.ok(content);  // ← 第113行
    }
}
```

> 🎯 置信度 **0.60**

## 🔵 INFO

### 14. 重复逻辑：DocumentBuilderFactory 构造与文档解析逻辑在两方法间重复 · `src/main/java/com/example/demo/XmlReportParser.java:20`

**问题**:parseReport（factory=builder=doc 解析）与 extractFields（factory=builder=doc 解析）构建了几乎相同的 DocumentBuilderFactory + DocumentBuilder + builder.parse 序列，仅后续遍历逻辑不同。这种重复把 XML 解析端点的配置（如 charset、factory 特性、异常处理）复制了两份——未来若需调整解析配置（如设置编码、安全特性或缓存 factory），需要同步修改两个方法，容易漏改导致行为不一致，属于知识库 DUPLICATION_DESIGN 中的一致性维护成本。建议提取共享的解析入口。

**建议**:抽取私有方法 List<Element> parseXml(String filePath, String tagName) 或返回 Document 的私有方法，parseReport 与 extractFields 复用它，集中 XML factory 配置与异常处理。

```java
...

    public List<Map<String, String>> parseReport(String filePath) throws Exception {
        List<Map<String, String>> records = new ArrayList<>();
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        DocumentBuilder builder = factory.newDocumentBuilder();  // ← 第20行
        Document doc = builder.parse(new InputSource(new FileReader(filePath)));
        NodeList items = doc.getElementsByTagName("item");
        for (int i = 0; i < items.getLength(); i++) {
            Element item = (Element) items.item(i);
...
```

> 🎯 置信度 **0.80**

### 15. 可观测性：方法签名直接抛出 Exception 且无日志，调用方无法区分错误性质 · `src/main/java/com/example/demo/XmlReportParser.java:17`

**问题**:三个方法都声明 `throws Exception`，把底层 IOException、SAXException、ParserConfigurationException 全部向上抛且不带任何业务上下文。调用方只能 catch Exception 而无法针对「文件不存在」「XML 格式非法」「权限不足」等不同失败分别处理，错误在日志/告警中也无法与具体解析请求关联，诊断困难。这符合 Plan 中「签名直接抛出 Exception 缺乏可观测性和调用方错误处理边界」的检查重点。建议定义领域异常并记录带文件路径的上下文日志。

**建议**:捕获底层异常并包裹为带文件路径上下文的业务异常（如 ReportParseException），必要时记录包含 filePath 的日志，暴露到调用方时可区分不同失败原因。

```java
...
 */
@Service
public class XmlReportParser {

    public List<Map<String, String>> parseReport(String filePath) throws Exception {  // ← 第17行
        List<Map<String, String>> records = new ArrayList<>();
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        DocumentBuilder builder = factory.newDocumentBuilder();
        Document doc = builder.parse(new InputSource(new FileReader(filePath)));
...
```

> 🎯 置信度 **0.75**

### 16. MINOR_SENSITIVE_DATA_IN_TOKEN · `src/main/java/com/example/demo/TokenService.java:21`

**问题**:generateToken 将原始 userId 明文拼入令牌前缀返回（'userId-随机32字符'）。若该 token 作为认证凭据随响应/请求传播，或写入日志，会暴露用户标识符。userId 通常本身即业务标识，且 token 后续使用按 '-' 拆分取 parts[1] 校验，parts[0] 即 userId 并未参与真实性校验，其明文暴露敏感度有限。该问题影响低，作为安全增强建议提示。

**建议**:令牌中不宜夹带明文 userId；若需关联用户，建议在 token payload 或服务端会话映射中携带，避免把业务标识直接拼入认证凭据字符串。

```java
...
        String chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        for (int i = 0; i < 32; i++) {
            sb.append(chars.charAt(random.nextInt(chars.length())));
        }
        return userId + "-" + sb.toString();  // ← 第21行
    }

    public boolean validateToken(String token) {
        try {
...
```

> 🎯 置信度 **0.50**

### 17. MISSING_AUTHORIZATION · `src/main/java/com/example/demo/UserController.java:28`

**问题**:整个 UserController（@RestController @RequestMapping("/users")）没有任何类级或方法级的认证/鉴权注解，search/export/import/by-domain/read-config/download-report 全部匿名可达。这些端点涉及文件读取（read-config、download-report）、文件写入（export）和用户数据查询（search、by-domain、import）。鉴权缺失放大上述路径穿越等风险的可利用性，且用户数据（PII）查询可被匿名访问。属既有设计缺陷，非本次 diff 引入的行为变化。

**建议**:为需要用户数据的端点增加鉴权与资源所有权校验（如 Spring Security + 认证主体），并限制文件操作端点仅授权用户可访问。

```java
...
    private final UserExportService userExportService;

    public UserController(UserService userService, UserExportService userExportService) {
        this.userService = userService;
        this.userExportService = userExportService;  // ← 第28行
    }

    /**
     * Search users by name.
...
```

> 🎯 置信度 **0.60**

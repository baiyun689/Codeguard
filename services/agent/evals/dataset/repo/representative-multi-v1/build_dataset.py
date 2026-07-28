"""Build a ten-project, multi-issue benchmark from frozen interview-v1 cases."""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[5]
SOURCE_ROOT = PROJECT_ROOT / ".eval-work" / "interview-v1" / "dataset" / "repo"
CASES_ROOT = ROOT / "cases"


@dataclass(frozen=True)
class Seed:
    file: str
    before: str
    after: str
    title: str
    dimension: str
    risk: str
    reviewers: tuple[str, ...]
    root_cause: str
    trigger: str
    consequence: str
    fix_action: str
    call_path: tuple[str, ...]
    keywords: tuple[str, ...]
    severity: str = "WARNING"


@dataclass(frozen=True)
class RealIssue:
    dimension: str
    risk: str
    reviewers: tuple[str, ...]
    trigger: str
    consequence: str
    fix_action: str
    call_path: tuple[str, ...]
    severity: str = "WARNING"


@dataclass(frozen=True)
class CaseSpec:
    source_id: str
    rationale: str
    real: RealIssue
    seeds: tuple[Seed, Seed]


def _seed(
    file: str,
    before: str,
    after: str,
    title: str,
    dimension: str,
    risk: str,
    reviewers: tuple[str, ...],
    root: str,
    trigger: str,
    consequence: str,
    fix: str,
    call_path: tuple[str, ...],
    keywords: tuple[str, ...],
    severity: str = "WARNING",
) -> Seed:
    return Seed(
        file=file,
        before=before,
        after=after,
        title=title,
        dimension=dimension,
        risk=risk,
        reviewers=reviewers,
        root_cause=root,
        trigger=trigger,
        consequence=consequence,
        fix_action=fix,
        call_path=call_path,
        keywords=keywords,
        severity=severity,
    )


def _real(
    dimension: str,
    risk: str,
    reviewers: tuple[str, ...],
    trigger: str,
    consequence: str,
    fix: str,
    call_path: tuple[str, ...],
    severity: str = "WARNING",
) -> RealIssue:
    return RealIssue(
        dimension, risk, reviewers, trigger, consequence, fix, call_path, severity
    )


CASES = (
    CaseSpec(
        "gitbug-spring-guice-injection",
        "Framework entry、泛型绑定与 Spring/Guice 双容器生命周期。",
        _real(
            "logic",
            "API_CONTRACT",
            ("behavior", "maintainability"),
            "通过自定义 binding annotation 请求 Spring bean",
            "Spring/Guice bridge 无法解析带自定义注解的依赖",
            "完整保留 binding annotation 并按 factory method 元数据生成 Guice Key",
            ("SpringModule.bind", "getAnnotationForBeanDefinition", "bindConditionally"),
        ),
        (
            _seed(
                "src/main/java/org/springframework/guice/module/GuiceAutowireCandidateResolver.java",
                """\
\t\t\t\tQualifier qualifierValue = qualifierBean(descriptor);
\t\t\t\tif (qualifierValue != null) {
\t\t\t\t\treturn Key.get(type, Names.named(qualifierValue.value()));
\t\t\t\t}
\t\t\t\treturn Key.get(type);""",
                """\
\t\t\t\tQualifier qualifierValue = qualifierBean(descriptor);
\t\t\t\tif (qualifierValue != null) {
\t\t\t\t\treturn Key.get(type);
\t\t\t\t}
\t\t\t\treturn Key.get(type);""",
                "延迟代理解析时丢失 Qualifier",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "DependencyDescriptor 中的 Qualifier 未进入 Guice Key",
                "同一接口存在两个命名实现且依赖通过延迟代理解析",
                "代理取得默认实现而不是注入点指定的实现",
                "以 qualifier 值构造带 Names.named 的 Key",
                (
                    "getLazyResolutionProxyIfNecessary",
                    "buildLazyResolutionProxy",
                    "guiceInstanceResolverKey",
                    "Injector.getInstance",
                ),
                ("Qualifier", "lazy proxy", "Guice Key"),
            ),
            _seed(
                "src/main/java/org/springframework/guice/annotation/GuiceFactoryBean.java",
                """\
\t@Override
\tpublic boolean isSingleton() {
\t\treturn this.isSingleton;
\t}""",
                """\
\t@Override
\tpublic boolean isSingleton() {
\t\treturn true;
\t}""",
                "FactoryBean 覆盖 Guice prototype scope",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "FactoryBean 无条件向 Spring 声明单例",
                "Guice Key 对应 prototype/no-scope binding 且被 Spring 多次请求",
                "首个实例被 Spring 缓存并错误复用于后续请求",
                "透传注册时计算出的 isSingleton 语义",
                ("GuiceModuleRegistrar", "GuiceFactoryBean.isSingleton", "BeanFactory"),
                ("FactoryBean", "scope", "prototype"),
            ),
        ),
    ),
    CaseSpec(
        "vul4j-42-command-injection",
        "OS 命令构造、文件遍历与归档解压三类高风险边界。",
        _real(
            "security",
            "INJECTION",
            ("threat_model",),
            "不可信参数进入 Commandline 和 BourneShell",
            "攻击者注入额外 shell 命令",
            "以参数数组执行并对 shell 元字符实施严格编码",
            ("Commandline.execute", "Shell.getShellCommandLine", "BourneShell.getExecutionPreamble"),
            "CRITICAL",
        ),
        (
            _seed(
                "src/main/java/org/codehaus/plexus/util/DirectoryScanner.java",
                "        this.followSymlinks = followSymlinks;",
                "        this.followSymlinks = true;",
                "扫描器忽略禁止跟随符号链接的配置",
                "security",
                "FILE_PATH_IO",
                ("threat_model", "behavior"),
                "setFollowSymlinks 丢弃调用方提供的安全策略",
                "构建目录中存在指向工作区外部的符号链接",
                "扫描结果包含根目录外文件并被后续打包或处理",
                "保存并执行调用方指定的 followSymlinks 值",
                ("DirectoryScanner.setFollowSymlinks", "scan", "scandir", "isSymbolicLink"),
                ("symlink", "directory scan", "workspace escape"),
                "CRITICAL",
            ),
            _seed(
                "src/main/java/org/codehaus/plexus/util/Expand.java",
                """\
                    while ( ( length =
                        compressedInputStream.read( buffer ) ) >= 0 )
                    {
                        fos.write( buffer, 0, length );
                    }""",
                """\
                    int extractedBytes = 0;
                    while ( ( length =
                        compressedInputStream.read( buffer ) ) >= 0 )
                    {
                        extractedBytes += length;
                        if ( extractedBytes > 100 * 1024 * 1024 )
                        {
                            throw new IOException( "Expanded entry is too large" );
                        }
                        fos.write( buffer, 0, length );
                    }""",
                "归档解压计数器使用可溢出的 int",
                "security",
                "PERFORMANCE",
                ("maintainability", "threat_model"),
                "单文件解压字节数以 int 累加且未在写入前实施可靠上限",
                "高压缩率条目解压后超过 Integer.MAX_VALUE",
                "计数回绕后资源限制失效并持续占用磁盘",
                "使用 long 累计总归档展开量并在写入前拒绝超限",
                ("Expand.expandFile", "extractFile", "ZipInputStream.read", "FileOutputStream.write"),
                ("zip bomb", "integer overflow", "disk exhaustion"),
                "CRITICAL",
            ),
        ),
    ),
    CaseSpec(
        "vul4j-43-path-traversal",
        "大型多模块 RDF4J，覆盖归档、远程加载与流式 I/O。",
        _real(
            "security",
            "FILE_PATH_IO",
            ("threat_model", "behavior"),
            "恶意 ZIP entry 使用父目录片段",
            "文件被写到目标目录之外",
            "规范化输出路径并验证仍位于目标根目录",
            ("RDFLoader.loadZip", "ZipUtil.extract", "Files.copy"),
            "CRITICAL",
        ),
        (
            _seed(
                "util/src/main/java/org/eclipse/rdf4j/common/io/IOUtil.java",
                """\
\tpublic static byte[] readBytes(InputStream in)
\t\tthrows IOException
\t{
\t\tByteArrayOutputStream out = new ByteArrayOutputStream(4096);
\t\ttransfer(in, out);
\t\treturn out.toByteArray();
\t}""",
                """\
\tpublic static byte[] readBytes(InputStream in)
\t\tthrows IOException
\t{
\t\treturn readBytes(in, in.available());
\t}""",
                "以 available() 推断完整流长度",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "InputStream.available 仅表示当前无阻塞可读字节而非总长度",
                "网络流或分段压缩流初次只暴露部分数据",
                "RDF 文档被静默截断并产生不完整仓库状态",
                "持续读取到 EOF，并由显式调用方预算控制最大字节数",
                ("RDFLoader.load", "IOUtil.readBytes", "InputStream.available"),
                ("InputStream", "available", "truncation"),
            ),
            _seed(
                "repository/api/src/main/java/org/eclipse/rdf4j/repository/util/RDFLoader.java",
                "\t\tURLConnection con = url.openConnection();",
                """\
\t\tURLConnection con = url.openConnection();
\t\tcon.setConnectTimeout(30_000);""",
                "远程 RDF 加载只限制建连而不限制读取",
                "quality",
                "RESOURCE_LIFECYCLE",
                ("maintainability",),
                "URLConnection 配置 connect timeout 后仍保留无限 read timeout",
                "远端接受连接后持续缓慢发送或停止响应",
                "工作线程和连接长期占用，批量导入无法释放容量",
                "同时配置有限 read timeout，并让上层提供统一请求期限",
                ("RDFLoader.load(URL)", "URLConnection.getInputStream", "RDFParser.parse"),
                ("read timeout", "slow response", "resource"),
            ),
        ),
    ),
    CaseSpec(
        "vul4j-48-jwt-validation",
        "JWT 结构、算法选择、签名验证和 registered claims 合约。",
        _real(
            "security",
            "INPUT_VALIDATION",
            ("threat_model", "behavior"),
            "提交结构不完整或额外分段的 JWT",
            "无效 token 进入解码和认证流程",
            "严格验证三段结构、签名存在性和算法约束",
            ("JWTDecoder.decode", "getParts", "Verifier.verify"),
            "CRITICAL",
        ),
        (
            _seed(
                "src/main/java/org/primeframework/jwt/hmac/HMACVerifier.java",
                """\
      if (!Arrays.equals(signature, actualSignature)) {
        throw new InvalidJWTSignatureException();
      }""",
                """\
      if (signature.length != actualSignature.length) {
        throw new InvalidJWTSignatureException();
      }""",
                "HMAC 校验只比较签名长度",
                "security",
                "AUTHENTICATION_SESSION",
                ("threat_model",),
                "Verifier 未比较攻击者签名与计算结果的内容",
                "攻击者提交任意与目标算法输出等长的签名",
                "伪造 JWT 被当作已认证 token 接受",
                "使用 MessageDigest.isEqual 做恒时完整字节比较",
                ("JWTDecoder.decode", "HMACVerifier.verify", "Mac.doFinal"),
                ("HMAC", "signature", "authentication"),
                "CRITICAL",
            ),
            _seed(
                "src/main/java/org/primeframework/jwt/domain/JWT.java",
                "    return rawClaims;",
                """\
    rawClaims.putAll(claims);
    return rawClaims;""",
                "自定义 claim 可覆盖 registered claim",
                "security",
                "API_CONTRACT",
                ("threat_model", "behavior"),
                "序列化末尾重新合并 claims，使 exp、iss、sub 等保留字段可被同名自定义值覆盖",
                "调用方同时设置 typed expiration/issuer 与同名自定义 claim",
                "签发内容与服务端校验、审计所见的主体或期限不一致",
                "拒绝保留名进入自定义 claims，并确保 typed claims 最后写入",
                ("JWT.addClaim", "JWT.getRawClaims", "JWTEncoder.encode"),
                ("claim collision", "registered claim", "JWT"),
                "CRITICAL",
            ),
        ),
    ),
    CaseSpec(
        "gitbug-spring-retry-interrupt",
        "并发重试、状态缓存、时间预算与线程中断传播。",
        _real(
            "logic",
            "ERROR_HANDLING",
            ("behavior",),
            "退避 sleep 期间线程收到 interrupt",
            "中断状态丢失，上层取消无法被观察",
            "重新设置 interrupt flag 后传播 BackOffInterruptedException",
            ("RetryTemplate", "BackOffPolicy.backOff", "Sleeper.sleep"),
        ),
        (
            _seed(
                "src/main/java/org/springframework/retry/policy/MapRetryContextCache.java",
                "private final Map<Object, RetryContext> map = Collections.synchronizedMap(new HashMap<>());",
                "private final Map<Object, RetryContext> map = new HashMap<>();",
                "有状态重试缓存退化为非线程安全 HashMap",
                "logic",
                "CONCURRENCY_CONSISTENCY",
                ("behavior",),
                "共享 RetryContextCache 在并发 get/put/remove 下没有同步",
                "多个工作线程以不同业务键并发执行 stateful retry",
                "缓存结构损坏、上下文串用或容量判断失真",
                "使用并发映射并以原子操作组合容量检查和写入",
                ("RetryTemplate", "RetryContextCache", "MapRetryContextCache.put"),
                ("retry cache", "concurrency", "state"),
            ),
            _seed(
                "src/main/java/org/springframework/retry/policy/TimeoutRetryPolicy.java",
                """\
	public TimeoutRetryPolicy(long timeout) {
		this.timeout = timeout;
	}""",
                """\
	public TimeoutRetryPolicy(long timeout) {
		this.timeout = (int) timeout;
	}""",
                "重试超时被窄化为 int",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "long 毫秒超时在构造时截断为 32 位有符号整数",
                "调用方配置超过 Integer.MAX_VALUE 毫秒的长周期任务",
                "超时变为负数或较小正数，重试立即终止或提前结束",
                "保持 long/Duration 类型并拒绝越界或负值",
                ("RetryTemplate.execute", "TimeoutRetryPolicy.open", "TimeoutRetryContext.isAlive"),
                ("timeout", "narrowing", "overflow"),
            ),
        ),
    ),
    CaseSpec(
        "gitbug-snowflake-credentials",
        "JDBC 连接串、敏感信息脱敏与指数退避边界。",
        _real(
            "security",
            "DATA_EXPOSURE",
            ("threat_model",),
            "包含凭据的连接 URL 解析失败",
            "异常消息和日志暴露用户名、密码或 token",
            "在构造异常前按参数语义统一脱敏连接属性",
            ("SnowflakeDriver.connect", "SnowflakeConnectString.parse", "SnowflakeSQLException"),
            "CRITICAL",
        ),
        (
            _seed(
                "src/main/java/net/snowflake/client/util/SecretDetector.java",
                "    return SENSITIVE_NAME_SET.contains(name.toLowerCase());",
                "    return SENSITIVE_NAME_SET.contains(name);",
                "混合大小写的敏感参数绕过脱敏",
                "security",
                "DATA_EXPOSURE",
                ("threat_model",),
                "敏感参数判断不再规范化调用方提供的名称",
                "驱动属性使用 Password、Private_Key 等非全小写名称",
                "诊断日志和异常序列化出原始凭据值",
                "以 Locale.ROOT 规范化名称后再匹配敏感字段集合",
                ("SnowflakeConnectString", "SecretDetector.isSensitive", "maskSecrets"),
                ("redaction", "case normalization", "credentials"),
                "CRITICAL",
            ),
            _seed(
                "src/main/java/net/snowflake/client/util/DecorrelatedJitterBackoff.java",
                "    return Math.min(cap, ThreadLocalRandom.current().nextLong(base, sleep * 3));",
                "    return ThreadLocalRandom.current().nextLong(base, sleep * 3);",
                "退避时间不再受 cap 约束",
                "logic",
                "IDEMPOTENCY_RETRY",
                ("behavior", "maintainability"),
                "下一次 sleep 直接取指数随机上界而没有应用最大等待时间",
                "连续网络失败使上一次 sleep 多轮增长",
                "重试线程长时间不可用，乘法溢出后还可能抛出参数异常",
                "在乘法溢出安全的前提下将结果限制到 cap",
                ("RetryingRestClient", "DecorrelatedJitterBackoff.nextSleepTime", "Thread.sleep"),
                ("backoff", "cap", "overflow"),
            ),
        ),
    ),
    CaseSpec(
        "gitbug-evalex-memory",
        "表达式词法分析、变量解析与运算符结合性。",
        _real(
            "quality",
            "PERFORMANCE",
            ("maintainability", "behavior"),
            "指数记号包含极大的十进制指数",
            "解析器分配超大 BigDecimal 并耗尽内存",
            "在构造数值前限制指数位数和可接受数值规模",
            ("Expression.evaluate", "Tokenizer.parseNumberToken", "BigDecimal"),
        ),
        (
            _seed(
                "src/main/java/com/ezylang/evalex/data/MapBasedDataAccessor.java",
                "      new TreeMap<>(String.CASE_INSENSITIVE_ORDER);",
                "      new TreeMap<>((left, right) -> left.toUpperCase().compareTo(right.toUpperCase()));",
                "变量名比较依赖默认 Locale",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "大小写无关比较器使用进程默认区域进行大写转换",
                "服务运行在土耳其语等具有特殊大小写规则的 Locale",
                "同一表达式变量在写入和读取时出现不可移植的缺失或碰撞",
                "使用 String.CASE_INSENSITIVE_ORDER 或 Locale.ROOT 规范化",
                ("Expression.with", "MapBasedDataAccessor.setData", "getData", "ASTNode.evaluate"),
                ("locale", "case insensitive", "variable lookup"),
            ),
            _seed(
                "src/main/java/com/ezylang/evalex/parser/ShuntingYardConverter.java",
                """\
      return currentOperator.getPrecedence(configuration)
          < nextOperator.getPrecedence(configuration);""",
                """\
      return currentOperator.getPrecedence(configuration)
          <= nextOperator.getPrecedence(configuration);""",
                "右结合运算符被按左结合归约",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "右结合分支在优先级相等时也弹出栈顶运算符",
                "表达式包含连续幂运算或其他同优先级右结合运算符",
                "生成的 AST 改变求值顺序并返回错误业务结果",
                "右结合分支仅在当前优先级严格低于栈顶时归约",
                ("Expression.evaluate", "ShuntingYardConverter.convert", "isNextOperatorOfHigherPrecedence"),
                ("associativity", "precedence", "AST"),
            ),
        ),
    ),
    CaseSpec(
        "gitbug-mcs-runtime-errors",
        "HTTP 搜索客户端、JSON 类型转换与结果打印链。",
        _real(
            "logic",
            "ERROR_HANDLING",
            ("behavior",),
            "HTTP、JSON 或映射阶段抛出运行时异常",
            "CLI 绕过 Result.Failure 合约并直接崩溃",
            "在 I/O 边界捕获可预期解析异常并保留原因返回 Failure",
            ("SearchClient.search", "SearchResponseBodyHandler.apply", "Result"),
        ),
        (
            _seed(
                "src/main/java/it/mulders/mcs/common/SearchResponseBodyHandler.java",
                '                (int) input.get("numFound"),',
                '                (short) (int) input.get("numFound"),',
                "搜索总数被窄化为 short",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "JSON 中的 int 结果数在领域响应构造时再次窄化为 16 位",
                "中央仓库查询命中超过 32767 个文档",
                "numFound 回绕为负数并破坏分页、输出模式和唯一结果判断",
                "保持协议字段的 int/long 宽度并校验非负范围",
                ("SearchClient.search", "SearchResponseBodyHandler.constructResponse", "CoordinatePrinter"),
                ("numeric narrowing", "numFound", "pagination"),
            ),
            _seed(
                "src/main/java/it/mulders/mcs/common/SearchResponseBodyHandler.java",
                """\
        return input.stream()
                .map(SearchResponseBodyHandler::constructDoc)""",
                """\
        return input.stream()
                .limit(20)
                .map(SearchResponseBodyHandler::constructDoc)""",
                "响应映射静默截断文档列表",
                "quality",
                "API_CONTRACT",
                ("behavior", "maintainability"),
                "BodyHandler 在协议解析层硬编码最多保留 20 个文档",
                "调用方请求大于 20 的结果页且服务端正常返回",
                "响应声明的总数与实际 docs 不一致，调用方静默丢失结果",
                "完整映射当前响应页，将分页上限留在查询构造层",
                ("SearchClient.search", "SearchResponseBodyHandler.constructDocs", "SearchResultPrinter"),
                ("silent truncation", "response mapping", "pagination"),
            ),
        ),
    ),
    CaseSpec(
        "gitbug-jaxb-uppercase",
        "XJC 插件跨包代码模型、注解查找与 ObjectFactory 聚合。",
        _real(
            "logic",
            "API_CONTRACT",
            ("behavior",),
            "生成类名以两个以上连续大写字母开头",
            "属性名归一化不符合 JAXB bean 命名规则",
            "按 JAXB/JavaBeans 规则处理连续大写前缀",
            ("Plugin.run", "Candidate", "CommonUtils"),
        ),
        (
            _seed(
                "src/main/java/com/sun/tools/xjc/addon/xew/CommonUtils.java",
                "			if (annotation.getAnnotationClass().equals(annotationClass)) {",
                "			if (annotation.getAnnotationClass().name().equals(annotationClass.name())) {",
                "注解查找只比较简单类名",
                "logic",
                "API_CONTRACT",
                ("behavior",),
                "代码模型注解匹配丢弃包名，仅比较 name",
                "模型同时出现来自不同包但同名的注解类型",
                "插件读取或修改错误注解，生成源码的序列化合约被改变",
                "使用 JClass 完整身份或 fullName 比较注解类型",
                ("Plugin.run", "CommonUtils.getAnnotation", "JAnnotatable.annotations"),
                ("annotation identity", "package", "code generation"),
            ),
            _seed(
                "src/main/java/com/sun/tools/xjc/addon/xew/Candidate.java",
                """\
		if (objectFactoryClasses.containsKey(valueObjectFactoryClass.fullName())) {
			return false;
		}

		objectFactoryClasses.put(valueObjectFactoryClass.fullName(), valueObjectFactoryClass);""",
                """\
		if (objectFactoryClasses.containsKey(valueObjectFactoryClass.name())) {
			return false;
		}

		objectFactoryClasses.put(valueObjectFactoryClass.name(), valueObjectFactoryClass);""",
                "不同包的 ObjectFactory 被错误去重",
                "quality",
                "API_CONTRACT",
                ("behavior", "maintainability"),
                "候选集合用固定简单类名 ObjectFactory 作为全局 key",
                "同一次 XJC 生成包含两个或更多 schema 包",
                "后续包的 factory 被跳过，部分元素声明无法进入生成代码",
                "以完整限定名或 package identity 作为候选键",
                ("Plugin.run", "Candidate.addObjectFactoryForClass", "objectFactoryClasses"),
                ("ObjectFactory", "package collision", "code generation"),
            ),
        ),
    ),
    CaseSpec(
        "gitbug-quality-cbor-type",
        "二进制协议长度解析、模型不变量与特殊类型分派。",
        _real(
            "quality",
            "API_CONTRACT",
            ("maintainability", "behavior"),
            "解码或编码遇到未分配的 special type",
            "模型暴露无法稳定往返编码的无效状态",
            "限制公开枚举到协议已分配值，并对未知值显式报错",
            ("CborDecoder.decodeNext", "SpecialDecoder.decode", "SpecialType"),
        ),
        (
            _seed(
                "src/main/java/co/nstant/in/cbor/model/RationalNumber.java",
                """\
        if (denominator.getValue().equals(BigInteger.ZERO)) {
            throw new CborException("Denominator is zero");
        }
""",
                "",
                "RationalNumber 接受零分母",
                "logic",
                "INPUT_VALIDATION",
                ("behavior",),
                "构造函数不再维护分母非零的领域不变量",
                "解码 tag 30 或调用公开构造器传入零分母",
                "无效有理数进入模型并在后续运算、编码或展示阶段延迟失败",
                "在模型边界拒绝零分母并保留明确 CborException",
                ("CborDecoder.decodeNext", "RationalNumber.<init>", "DataItem.add"),
                ("rational", "zero denominator", "invariant"),
            ),
            _seed(
                "src/main/java/co/nstant/in/cbor/decoder/ArrayDecoder.java",
                "        for (long i = 0; i < length; i++) {",
                """\
        int itemCount = (int) length;
        for (int i = 0; i < itemCount; i++) {""",
                "CBOR 数组长度窄化后控制解码循环",
                "security",
                "INPUT_VALIDATION",
                ("threat_model", "behavior"),
                "协议中的 64 位数组长度未经范围验证转换为有符号 int",
                "输入声明超过 Integer.MAX_VALUE 的 definite-length 数组",
                "循环可能零次或提前结束，剩余字节被当作后续顶层对象解析",
                "拒绝超出实现预算的长度并保持 long 作为循环边界",
                ("CborDecoder.decodeNext", "ArrayDecoder.decode", "decodeFixedLength"),
                ("CBOR length", "integer narrowing", "parser desync"),
                "CRITICAL",
            ),
        ),
    ),
)


def _replace_once(text: str, before: str, after: str, label: str) -> str:
    count = text.count(before)
    if count != 1:
        raise ValueError(f"{label}: expected one replacement target, found {count}")
    return text.replace(before, after, 1)


def _git_diff(path: str, before: str, after: str) -> str:
    body = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=4,
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}"


def _line_number(text: str, changed: str) -> int:
    anchors = [line.strip() for line in changed.splitlines() if line.strip()]
    for anchor in anchors:
        for index, line in enumerate(text.splitlines(), start=1):
            if line.strip() == anchor:
                return index
    return 0


def _issue_from_seed(index: int, seed: Seed, final_text: str) -> dict:
    return {
        "id": f"E{index}",
        "origin": "controlled-seed",
        "title": seed.title,
        "dimension": seed.dimension,
        "severity": seed.severity,
        "file": seed.file,
        "line": _line_number(final_text, seed.after or seed.before),
        "root_cause": seed.root_cause,
        "trigger": seed.trigger,
        "observable_consequence": seed.consequence,
        "fix_action": seed.fix_action,
        "call_path": list(seed.call_path),
        "primary_risk_tag": seed.risk,
        "expected_reviewers": list(seed.reviewers),
        "type_keywords": list(seed.keywords),
    }


def _write_yaml(path: Path, value: dict) -> None:
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _copy_repo(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(".git", "target", ".idea", "*.class"),
    )


def _build_case(spec: CaseSpec) -> dict:
    source_dir = SOURCE_ROOT / spec.source_id
    case_dir = CASES_ROOT / spec.source_id
    repo_dir = case_dir / "repo"
    case_dir.mkdir(parents=True)
    _copy_repo(source_dir / "repo", repo_dir)

    original_case = yaml.safe_load(
        (source_dir / "case.yaml").read_text(encoding="utf-8")
    )
    original_expected = original_case["expected"][0]
    changed_files: dict[str, tuple[str, str]] = {}
    seed_issues: list[dict] = []
    for index, seed in enumerate(spec.seeds, start=2):
        target = repo_dir / seed.file
        before_file = target.read_text(encoding="utf-8")
        after_file = _replace_once(
            before_file, seed.before, seed.after, f"{spec.source_id}/{seed.file}"
        )
        target.write_text(after_file, encoding="utf-8")
        if seed.file in changed_files:
            initial, _ = changed_files[seed.file]
            changed_files[seed.file] = (initial, after_file)
        else:
            changed_files[seed.file] = (before_file, after_file)
        seed_issues.append(_issue_from_seed(index, seed, after_file))

    seeded_diff = "".join(
        _git_diff(path, before, after)
        for path, (before, after) in sorted(changed_files.items())
    )
    upstream_diff = (source_dir / "changes.diff").read_text(encoding="utf-8")
    if upstream_diff and not upstream_diff.endswith("\n"):
        upstream_diff += "\n"
    (case_dir / "seeded.diff").write_text(seeded_diff, encoding="utf-8")
    (case_dir / "changes.diff").write_text(
        upstream_diff + seeded_diff, encoding="utf-8"
    )

    real_issue = {
        "id": "E1",
        "origin": "upstream-real",
        "title": original_case["category"],
        "dimension": spec.real.dimension,
        "severity": spec.real.severity,
        "file": original_expected["file"],
        "line": original_expected.get("line", 0),
        "root_cause": original_expected["root_cause"],
        "trigger": spec.real.trigger,
        "observable_consequence": spec.real.consequence,
        "fix_action": spec.real.fix_action,
        "call_path": list(spec.real.call_path),
        "primary_risk_tag": spec.real.risk,
        "expected_reviewers": list(spec.real.reviewers),
        "type_keywords": original_expected["type_keywords"],
    }
    issues = [real_issue, *seed_issues]
    enhanced_case = {
        "id": spec.source_id,
        "category": "representative multi-issue PR",
        "description": spec.rationale,
        "ground_truth_mode": "complete-issue-set",
        "difficulty": "deep",
        "provenance": {
            **original_case["provenance"],
            "source_case": spec.source_id,
            "enhancement": "two controlled issues added to the frozen vulnerable snapshot",
        },
        "expected": [
            {
                "id": issue["id"],
                "origin": issue["origin"],
                "file": issue["file"],
                "line": issue["line"],
                "type_keywords": issue["type_keywords"],
                "root_cause": issue["root_cause"],
                "risk_tag": issue["primary_risk_tag"],
                "tolerance": 5 if issue["origin"] == "controlled-seed" else 0,
            }
            for issue in issues
        ],
    }
    _write_yaml(case_dir / "case.yaml", enhanced_case)
    _write_yaml(
        case_dir / "ground-truth.yaml",
        {"case_id": spec.source_id, "issues": issues},
    )
    oracle_dir = case_dir / "oracle-tests"
    oracle_dir.mkdir()
    for issue in issues:
        _write_yaml(
            oracle_dir / f"{issue['id'].lower()}.yaml",
            {
                "issue_id": issue["id"],
                "not_exposed_to_review_model": True,
                "trigger": issue["trigger"],
                "expected_observation": issue["observable_consequence"],
                "source_anchor": {
                    "file": issue["file"],
                    "line": issue["line"],
                    "call_path": issue["call_path"],
                },
            },
        )
    return {
        "id": spec.source_id,
        "repository_url": original_case["provenance"]["repository_url"],
        "source_case": spec.source_id,
        "issue_count": len(issues),
    }


def build() -> None:
    if CASES_ROOT.exists():
        resolved = CASES_ROOT.resolve()
        if resolved.parent != ROOT.resolve():
            raise RuntimeError(f"refusing to replace unexpected path: {resolved}")
        shutil.rmtree(resolved)
    CASES_ROOT.mkdir(parents=True)
    built_cases = [_build_case(spec) for spec in CASES]
    _write_yaml(
        ROOT / "manifest.yaml",
        {
            "id": "representative-multi-v1",
            "benchmark_type": "controlled project-level benchmark",
            "case_count": len(built_cases),
            "issue_count": sum(item["issue_count"] for item in built_cases),
            "selection": "ten representative, unique real Java repositories from interview-v1",
            "cases": built_cases,
        },
    )
    (ROOT / "README.md").write_text(
        """# representative-multi-v1

该数据集从冻结的 `interview-v1` 中选出 10 个不同真实 Java 项目。每个 case
保留原始 reversed-fix 问题，并在完整项目快照中加入两个彼此独立的受控问题。

- `repo/`：PR 后完整项目快照。
- `changes.diff`：原始真实 diff 与新增受控 diff 的有效 Git unified diff。
- `seeded.diff`：仅包含两个新增问题，便于审计数据构造，不向审查模型单独暴露。
- `ground-truth.yaml`：三条完整金标。
- `oracle-tests/`：触发条件与可观察结果契约，不放入 `repo/`。

源码中不加入说明问题性质的注释。该基准用于受控项目级评测，不宣称新增问题来自
真实上游 PR。
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    build()

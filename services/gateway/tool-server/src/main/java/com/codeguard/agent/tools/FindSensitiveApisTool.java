package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 扫描 diff 涉及文件中的危险 API 调用(安全审查员专属工具)。
 * <p>
 * 用 JavaParser 遍历每个 diff 文件的方法调用节点,与预置的危险 API 清单(按全限定名前缀匹配)
 * 比对,返回 (API名, 文件, 行号, 参数片段, 危险等级) 列表。
 * 受 {@link FileAccessSandbox} 护栏约束:只扫描 repo 内的源码文件,拒绝穿越。
 */
public final class FindSensitiveApisTool implements AgentTool {

    /** 危险等级 */
    public enum Risk {
        HIGH, MEDIUM
    }

    /**
     * 危险 API 条目:全限定名前缀 + 等级。
     * <p>
     * 匹配方式:前缀匹配。例如 "java.sql.Statement.execute" 会匹配
     * "java.sql.Statement.executeQuery" 和 "java.sql.Statement.executeUpdate"。
     */
    private record SensitiveApi(String fqnPrefix, Risk risk) {}

    /** 危险 API 清单(按类分组便于维护) */
    private static final List<SensitiveApi> SENSITIVE_APIS = buildList();

    private static List<SensitiveApi> buildList() {
        List<SensitiveApi> list = new ArrayList<>();

        // SQL 执行 —— 字符串拼接 SQL 有注入风险
        list.add(new SensitiveApi("java.sql.Statement.execute", Risk.HIGH));
        list.add(new SensitiveApi("java.sql.Connection.prepareStatement", Risk.MEDIUM));
        list.add(new SensitiveApi("javax.persistence.EntityManager.createQuery", Risk.MEDIUM));
        list.add(new SensitiveApi("javax.persistence.EntityManager.createNativeQuery", Risk.MEDIUM));
        list.add(new SensitiveApi("org.springframework.jdbc.core.JdbcTemplate", Risk.MEDIUM));

        // 命令执行
        list.add(new SensitiveApi("java.lang.Runtime.exec", Risk.HIGH));
        list.add(new SensitiveApi("java.lang.ProcessBuilder.start", Risk.HIGH));
        list.add(new SensitiveApi("java.lang.ProcessBuilder.command", Risk.HIGH));

        // 反序列化
        list.add(new SensitiveApi("java.io.ObjectInputStream.readObject", Risk.HIGH));
        list.add(new SensitiveApi("java.io.ObjectInputStream.readUnshared", Risk.HIGH));
        list.add(new SensitiveApi("com.fasterxml.jackson.databind.ObjectMapper.readValue", Risk.MEDIUM));
        list.add(new SensitiveApi("com.fasterxml.jackson.databind.ObjectMapper.readTree", Risk.MEDIUM));

        // 弱加密算法
        list.add(new SensitiveApi("java.security.MessageDigest.getInstance", Risk.HIGH));
        list.add(new SensitiveApi("javax.crypto.Cipher.getInstance", Risk.HIGH));
        list.add(new SensitiveApi("javax.crypto.KeyGenerator.getInstance", Risk.HIGH));
        list.add(new SensitiveApi("javax.crypto.SecretKeyFactory.getInstance", Risk.MEDIUM));

        // 路径/文件操作
        list.add(new SensitiveApi("java.io.File.delete", Risk.MEDIUM));
        list.add(new SensitiveApi("java.nio.file.Files.delete", Risk.MEDIUM));
        list.add(new SensitiveApi("java.nio.file.Files.copy", Risk.MEDIUM));
        list.add(new SensitiveApi("java.nio.file.Files.move", Risk.MEDIUM));
        list.add(new SensitiveApi("java.nio.file.Files.createFile", Risk.MEDIUM));
        list.add(new SensitiveApi("java.nio.file.Files.newInputStream", Risk.MEDIUM));
        list.add(new SensitiveApi("java.io.RandomAccessFile", Risk.MEDIUM));

        // 反射
        list.add(new SensitiveApi("java.lang.reflect.Method.invoke", Risk.MEDIUM));
        list.add(new SensitiveApi("java.lang.Class.forName", Risk.MEDIUM));
        list.add(new SensitiveApi("java.lang.Class.newInstance", Risk.MEDIUM));

        // 脚本执行
        list.add(new SensitiveApi("javax.script.ScriptEngine.eval", Risk.HIGH));

        // XML (XXE)
        list.add(new SensitiveApi("javax.xml.parsers.DocumentBuilder.parse", Risk.MEDIUM));
        list.add(new SensitiveApi("javax.xml.parsers.SAXParser.parse", Risk.MEDIUM));
        list.add(new SensitiveApi("javax.xml.transform.TransformerFactory.newInstance", Risk.MEDIUM));

        // URL / SSRF
        list.add(new SensitiveApi("java.net.URL.openConnection", Risk.MEDIUM));
        list.add(new SensitiveApi("java.net.URL.openStream", Risk.MEDIUM));
        list.add(new SensitiveApi("java.net.URI.create", Risk.MEDIUM));

        // 日志注入
        list.add(new SensitiveApi("java.util.logging.Logger", Risk.MEDIUM));
        list.add(new SensitiveApi("org.slf4j.Logger", Risk.MEDIUM));

        return List.copyOf(list);
    }

    private final FileAccessSandbox sandbox;

    public FindSensitiveApisTool(FileAccessSandbox sandbox) {
        this.sandbox = sandbox;
    }

    @Override
    public String name() {
        return "find_sensitive_apis";
    }

    @Override
    public String description() {
        return "系统性地扫描本次 diff 涉及的所有文件,发现其中的危险 API 调用(SQL 执行/命令注入/反序列化/弱加密/"
                + "路径操作/反射/脚本执行/XXE/SSRF)。无需入参——工具自动扫描当前会话的源文件。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        Set<String> allowedFiles = context.getAllowedFiles();
        if (allowedFiles.isEmpty()) {
            return ToolResult.ok("# 敏感 API 扫描\n(无可扫描的 diff 文件)\n");
        }

        List<String> findings = new ArrayList<>();
        int scanned = 0;
        int skipped = 0;
        int total = 0;

        for (String relPath : allowedFiles) {
            if (!sandbox.isReadableSource(relPath)) {
                continue;
            }
            scanned++;
            Path fullPath;
            try {
                fullPath = sandbox.resolveWithinRepo(relPath);
            } catch (SecurityException e) {
                skipped++;
                continue;
            }
            if (!Files.isRegularFile(fullPath)) {
                skipped++;
                continue;
            }

            String source;
            try {
                source = Files.readString(fullPath, StandardCharsets.UTF_8);
            } catch (IOException e) {
                skipped++;
                continue;
            }

            List<String> fileFindings = scanSource(relPath, source);
            findings.addAll(fileFindings);
            total += fileFindings.size();
        }

        StringBuilder sb = new StringBuilder();
        sb.append("# 敏感 API 扫描\n");
        sb.append(String.format("扫描 %d 个文件, 跳过 %d 个不可解析文件, 发现 %d 处敏感 API 调用\n\n", scanned, skipped, total));
        if (findings.isEmpty()) {
            sb.append("未发现危险 API 调用。\n");
        } else {
            sb.append("| 危险等级 | API | 文件 | 行号 | 调用参数 |\n");
            sb.append("|---------|-----|------|------|----------|\n");
            for (String f : findings) {
                sb.append(f).append("\n");
            }
        }
        return ToolResult.ok(sb.toString());
    }

    private List<String> scanSource(String relPath, String source) {
        List<String> results = new ArrayList<>();
        CompilationUnit cu;
        try {
            ParseResult<CompilationUnit> result = new JavaParser().parse(source);
            if (!result.isSuccessful() || result.getResult().isEmpty()) {
                return results;
            }
            cu = result.getResult().get();
        } catch (Exception e) {
            return results;
        }

        // 扫描 MethodCallExpr
        cu.findAll(MethodCallExpr.class).forEach(call -> {
            String methodName = call.getNameAsString();
            // 取出 scope 的类型名(如 obj.method() 中的 obj 的类型)
            String scopeType = extractScopeType(call);
            // 拼接:scope.methodName
            String callName = scopeType.isEmpty() ? methodName : scopeType + "." + methodName;
            for (SensitiveApi api : SENSITIVE_APIS) {
                if (callName.endsWith(api.fqnPrefix.substring(api.fqnPrefix.lastIndexOf('.') + 1))
                        || matchesFqn(callName, scopeType, methodName, api.fqnPrefix)) {
                    int line = call.getBegin().map(p -> p.line).orElse(-1);
                    String args = abbreviateArgs(call);
                    results.add(formatFinding(api.risk, callName, relPath, line, args));
                    break;
                }
            }
        });

        // 扫描 ObjectCreationExpr (new Xxx(...))
        cu.findAll(ObjectCreationExpr.class).forEach(neu -> {
            String typeName = neu.getType().getNameAsString();
            for (SensitiveApi api : SENSITIVE_APIS) {
                String fqnShort = api.fqnPrefix.substring(api.fqnPrefix.lastIndexOf('.') + 1);
                if (typeName.equals(fqnShort) || api.fqnPrefix.endsWith("." + typeName)) {
                    int line = neu.getBegin().map(p -> p.line).orElse(-1);
                    String args = abbreviateArgs(neu);
                    results.add(formatFinding(api.risk, "new " + typeName, relPath, line, args));
                    break;
                }
            }
        });

        return results;
    }

    /**
     * 尝试从方法调用表达式中提取 scope 的类型名。
     * JavaParser 的 MethodCallExpr.getScope() 返回 Optional<Expression>。
     */
    private String extractScopeType(MethodCallExpr call) {
        return call.getScope()
                .map(Expression::toString)
                .orElse("");
    }

    /**
     * 匹配 FQN 前缀:方法名支持前缀匹配(如 executeQuery 匹配 execute 前缀)。
     * scope 匹配采用宽松策略:由于没有完整类型解析,变量名可能不等于类名(如 stmt vs Statement),
     * 因此只要方法名匹配就认定命中——LLM 审查员会通过 get_file_content 进一步核实。
     */
    private boolean matchesFqn(String callName, String scopeType, String methodName, String fqnPrefix) {
        String fqnMethod = fqnPrefix.substring(fqnPrefix.lastIndexOf('.') + 1);
        // 方法名前缀匹配:executeQuery 匹配 execute, readObject 匹配 readObject
        if (methodName.startsWith(fqnMethod) || methodName.equals(fqnMethod)) {
            return true;
        }
        return false;
    }

    private String abbreviateArgs(MethodCallExpr call) {
        List<String> parts = new ArrayList<>();
        for (Expression arg : call.getArguments()) {
            if (arg instanceof StringLiteralExpr sl) {
                parts.add("\"" + truncate(sl.getValue(), 40) + "\"");
            } else {
                parts.add(truncate(arg.toString(), 30));
            }
        }
        return String.join(", ", parts);
    }

    private String abbreviateArgs(ObjectCreationExpr neu) {
        List<String> parts = new ArrayList<>();
        for (Expression arg : neu.getArguments()) {
            if (arg instanceof StringLiteralExpr sl) {
                parts.add("\"" + truncate(sl.getValue(), 40) + "\"");
            } else {
                parts.add(truncate(arg.toString(), 30));
            }
        }
        return String.join(", ", parts);
    }

    private String formatFinding(Risk risk, String api, String file, int line, String args) {
        return String.format("| %s | `%s` | %s:%d | `%s` |",
                risk == Risk.HIGH ? "🔴 HIGH" : "🟡 MEDIUM",
                api, file, line, args);
    }

    private static String truncate(String s, int maxLen) {
        if (s == null) return "";
        return s.length() <= maxLen ? s : s.substring(0, maxLen) + "...";
    }
}

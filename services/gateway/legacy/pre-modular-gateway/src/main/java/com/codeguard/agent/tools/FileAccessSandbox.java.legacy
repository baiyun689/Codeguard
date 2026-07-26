package com.codeguard.agent.tools;

import java.nio.file.Path;
import java.util.Set;

/**
 * 文件访问安全沙箱 —— 护栏层的核心。
 * <p>
 * 约束 Agent 通过工具读文件时只能触及"该看的东西",防止越权读取任意文件:
 * <ul>
 *   <li>禁止路径穿越:规范化后必须仍位于仓库根目录内;</li>
 *   <li>源码白名单:只允许读 repo 根内、扩展名属于源码类型的文件(见 {@link #SOURCE_EXTENSIONS})。</li>
 * </ul>
 * <p>
 * 护栏已放宽:审查员可读取 repo 根内任意源码文件(受扩展名白名单约束),
 * 不再限制为仅 diff 文件。仍保留路径穿越防御与(由 {@link GetFileContentTool} 施加的)大小上限。
 * <p>
 * 路径比对统一规范化为相对仓库根的**正斜杠**相对路径,以兼容 Windows 反斜杠。
 * 本类只做"判定",不读文件;读取与大小限制由 {@link GetFileContentTool} 负责。
 */
public final class FileAccessSandbox {

    /** 可读文件扩展名白名单(小写,不含点)。涵盖源码及常见构建/配置文件。 */
    private static final Set<String> SOURCE_EXTENSIONS = Set.of(
            "java", "kt", "kts", "scala", "groovy",
            "js", "jsx", "ts", "tsx", "py", "go", "rb", "rs",
            "c", "h", "cpp", "hpp", "cc", "cs",
            "xml", "yml", "yaml", "properties", "toml", "json", "gradle", "mf");

    private final Path repoRoot;
    private final Set<String> allowedFiles;

    public FileAccessSandbox(Path repoRoot, Set<String> allowedFiles) {
        this.repoRoot = repoRoot.normalize().toAbsolutePath();
        this.allowedFiles = Set.copyOf(allowedFiles);
    }

    /**
     * 把相对路径解析为仓库内的绝对路径,并校验未穿越出仓库根。
     *
     * @return 仓库内的规范化绝对路径
     * @throws SecurityException 路径穿越(规范化后逃逸出仓库根)
     */
    public Path resolveWithinRepo(String relativePath) throws SecurityException {
        Path resolved = repoRoot.resolve(relativePath).normalize().toAbsolutePath();
        if (!resolved.startsWith(repoRoot)) {
            throw new SecurityException("路径超出仓库范围: " + relativePath);
        }
        return resolved;
    }

    /** 该相对路径是否落在本次 diff 的允许文件集合内。 */
    public boolean isFileInScope(String relativePath) {
        Path resolved;
        try {
            resolved = resolveWithinRepo(relativePath);
        } catch (SecurityException e) {
            return false;
        }
        String normalized = repoRoot.relativize(resolved).toString().replace('\\', '/');
        return allowedFiles.contains(normalized);
    }

    /**
     * 该相对路径是否为 repo 根内、可读的源码文件(穿越防御 + 源码扩展名白名单)。
     * 这是放宽后 get_file_content 的读授权判据(design.md D5)。
     */
    public boolean isReadableSource(String relativePath) {
        Path resolved;
        try {
            resolved = resolveWithinRepo(relativePath);
        } catch (SecurityException e) {
            return false;
        }
        String name = resolved.getFileName().toString();
        int dot = name.lastIndexOf('.');
        if (dot < 0 || dot == name.length() - 1) {
            return false;
        }
        return SOURCE_EXTENSIONS.contains(name.substring(dot + 1).toLowerCase());
    }

    public Path getRepoRoot() {
        return repoRoot;
    }
}

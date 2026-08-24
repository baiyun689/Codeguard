package com.codeguard.agent.tools;

import java.io.IOException;
import java.nio.file.Files;
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
 * 路径校验基于真实路径而非词法路径：允许链接指向仓库内文件，但链接最终落点不能逃出仓库。
 * 本类只做授权判定，不读文件内容；读取与大小限制由 {@link GetFileContentTool} 负责。
 */
public final class FileAccessSandbox {

    /** 可读文件扩展名白名单(小写,不含点)。涵盖源码及常见构建/配置文件。 */
    private static final Set<String> SOURCE_EXTENSIONS = Set.of(
            "java", "kt", "kts", "scala", "groovy",
            "js", "jsx", "ts", "tsx", "py", "go", "rb", "rs",
            "c", "h", "cpp", "hpp", "cc", "cs",
            "xml", "yml", "yaml", "properties", "toml", "json", "gradle", "mf");

    private final Path repoRoot;
    private final Path realRepoRoot;

    public FileAccessSandbox(Path repoRoot) {
        this.repoRoot = repoRoot.normalize().toAbsolutePath();
        try {
            this.realRepoRoot = this.repoRoot.toRealPath();
        } catch (IOException exception) {
            throw new IllegalArgumentException("审查仓库不可访问", exception);
        }
    }

    /**
     * 解析一个允许读取的文件。所有调用方必须使用本方法，不能先解析、再自行补校验。
     *
     * @return 最终可读取文件的真实路径
     */
    public Path resolveReadableFile(String relativePath) throws AccessException {
        if (relativePath == null || relativePath.isBlank()) {
            throw new AccessException("rejected_invalid_path");
        }
        final Path requested;
        try {
            requested = Path.of(relativePath.trim());
        } catch (Exception exception) {
            throw new AccessException("rejected_invalid_path");
        }
        if (requested.isAbsolute()) {
            throw new AccessException("rejected_invalid_path");
        }
        Path lexical = repoRoot.resolve(requested).normalize().toAbsolutePath();
        if (!lexical.startsWith(repoRoot)) {
            throw new AccessException("rejected_path_outside_repository");
        }
        final Path real;
        try {
            real = lexical.toRealPath();
        } catch (java.nio.file.NoSuchFileException exception) {
            throw new AccessException("missing_file");
        } catch (IOException exception) {
            throw new AccessException("rejected_invalid_path");
        }
        if (!real.startsWith(realRepoRoot)) {
            throw new AccessException("rejected_path_outside_repository");
        }
        if (!Files.isRegularFile(real)) {
            throw new AccessException("missing_file");
        }
        if (!hasReadableExtension(real)) {
            throw new AccessException("rejected_file_type");
        }
        return real;
    }

    private static boolean hasReadableExtension(Path resolved) {
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

    public Path getRealRepoRoot() {
        return realRepoRoot;
    }

    /** 稳定的工具错误码，避免把外部路径或 IO 细节暴露给 Agent。 */
    public static final class AccessException extends Exception {
        public AccessException(String code) {
            super(code);
        }
    }
}

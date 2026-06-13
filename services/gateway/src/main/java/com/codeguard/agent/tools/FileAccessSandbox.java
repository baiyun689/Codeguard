package com.codeguard.agent.tools;

import java.nio.file.Path;
import java.util.Set;

/**
 * 文件访问安全沙箱 —— 护栏层的核心。
 * <p>
 * 约束 Agent 通过工具读文件时只能触及"本次审查该看的东西",防止越权读取任意文件:
 * <ul>
 *   <li>禁止路径穿越:规范化后必须仍位于仓库根目录内;</li>
 *   <li>白名单:只允许读本次 diff 涉及的文件集合(allowedFiles)内的文件;</li>
 * </ul>
 * 路径比对统一规范化为相对仓库根的**正斜杠**相对路径,以兼容 Windows 反斜杠。
 * 本类只做"判定",不读文件;读取与大小限制由 {@link GetFileContentTool} 负责。
 */
public final class FileAccessSandbox {

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

    public Path getRepoRoot() {
        return repoRoot;
    }
}

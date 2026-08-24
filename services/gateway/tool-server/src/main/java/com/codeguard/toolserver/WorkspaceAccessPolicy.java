package com.codeguard.toolserver;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * Tool Server 创建会话时的工作区准入策略。
 *
 * <p>请求方提供的 repo_path 不是可信边界；只有落在配置的工作区根目录之下，且本身是 Git
 * 工作区的目录才能成为审查会话根。真实路径校验会消除工作区根或 repo_path 中的符号链接歧义。</p>
 */
public final class WorkspaceAccessPolicy {
    private final List<Path> realAllowedRoots;

    public WorkspaceAccessPolicy(List<Path> allowedRoots) {
        if (allowedRoots == null || allowedRoots.isEmpty()) {
            throw new IllegalArgumentException("CODEGUARD_TOOL_ALLOWED_ROOTS 不能为空");
        }
        this.realAllowedRoots = allowedRoots.stream()
                .map(WorkspaceAccessPolicy::realDirectory)
                .toList();
    }

    /** 返回可安全作为会话根的真实路径，失败时不泄漏宿主机路径。 */
    public Path requireReviewRepository(Path requestedRoot) {
        if (requestedRoot == null) {
            throw new RejectedWorkspaceException("invalid_repository_workspace");
        }
        final Path realRepository;
        try {
            realRepository = requestedRoot.toRealPath();
        } catch (IOException exception) {
            throw new RejectedWorkspaceException("invalid_repository_workspace");
        }
        if (!Files.isDirectory(realRepository)
                || realAllowedRoots.stream().noneMatch(realRepository::startsWith)
                || !isGitWorktree(realRepository)) {
            throw new RejectedWorkspaceException("invalid_repository_workspace");
        }
        return realRepository;
    }

    private static Path realDirectory(Path root) {
        try {
            Path realRoot = root.toRealPath();
            if (!Files.isDirectory(realRoot)) {
                throw new IllegalArgumentException("CODEGUARD_TOOL_ALLOWED_ROOTS 必须是目录");
            }
            return realRoot;
        } catch (IOException exception) {
            throw new IllegalArgumentException("CODEGUARD_TOOL_ALLOWED_ROOTS 包含不可访问目录", exception);
        }
    }

    private static boolean isGitWorktree(Path repository) {
        Path gitMarker = repository.resolve(".git");
        return Files.isDirectory(gitMarker) || Files.isRegularFile(gitMarker);
    }

    public static final class RejectedWorkspaceException extends IllegalArgumentException {
        RejectedWorkspaceException(String code) {
            super(code);
        }
    }
}

package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.repomap.RepoMapBuilder;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * get_repo_map 端到端:在临时仓库上,被改文件引用了定义在别处的符号,
 * 地图应指向那个 diff 之外的定义文件。空仓库返回可读提示而非报错。
 */
class GetRepoMapToolTest {

    private final GetRepoMapTool tool = new GetRepoMapTool(new RepoMapBuilder());

    @Test
    void mapSurfacesDiffExternalDefinition(@TempDir Path repo) throws IOException {
        // 被改文件:调用了 SecurityUtils.sanitize —— 定义不在本文件。
        Path changed = repo.resolve("src/FileController.java");
        Files.createDirectories(changed.getParent());
        Files.writeString(changed, """
                package demo;
                class FileController {
                    String handle(String name) {
                        return SecurityUtils.sanitize(name);
                    }
                }
                """);
        // diff 之外的定义文件。
        Path util = repo.resolve("src/SecurityUtils.java");
        Files.writeString(util, """
                package demo;
                class SecurityUtils {
                    static String sanitize(String path) {
                        if (path.contains("..")) throw new SecurityException();
                        return path;
                    }
                }
                """);

        Set<String> seed = Set.of("src/FileController.java");
        ToolResult r = tool.execute("", new AgentContext(repo, seed));

        assertTrue(r.isSuccess());
        String map = r.getResult();
        assertTrue(map.contains("SecurityUtils.java"), "地图应指向 diff 之外的定义文件");
        assertTrue(map.contains("sanitize"), "地图应含被引用的 sanitize 定义");
        assertFalse(map.contains("FileController.java:"), "不应把 diff 改动文件自身的定义列入地图");
    }

    @Test
    void emptyRepoReturnsReadableHintNotError(@TempDir Path repo) {
        ToolResult r = tool.execute("", new AgentContext(repo, Set.of()));
        assertTrue(r.isSuccess(), "空仓库不报错");
        assertTrue(r.getResult().contains("get_file_content") || r.getResult().contains("未在仓库"),
                "返回可读提示");
    }
}

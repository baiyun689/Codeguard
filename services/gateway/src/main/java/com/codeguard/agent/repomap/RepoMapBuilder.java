package com.codeguard.agent.repomap;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.stream.Stream;

/**
 * 串联 repo map 全流程:扫仓库 Java 源文件 → 抽 tag → 排名 → 渲染。
 * <p>
 * 这是 Java 侧"地面真值/重计算"的一次性装配(无状态、可复用):
 * 扫描读取仓库源文件属于 Java 护栏层内部的事实采集(只对外暴露签名级地图,不外泄文件内容),
 * 与 get_file_content 的逐文件授权是两件事(见 design.md D5)。
 * <p>
 * 防爆:跳过构建产物/VCS 目录、限文件数与单文件大小、按路径排序保证确定性。
 */
public final class RepoMapBuilder {

    /** 参与建图的文件数上限,防大仓库扫描爆炸(diff-scoped 排名仍只突出邻域)。 */
    private static final int MAX_FILES = 2000;
    /** 单文件大小上限:超过跳过(生成代码/超大文件对建图无益)。 */
    private static final long MAX_FILE_BYTES = 1_000_000L;
    /** 扫描时跳过的目录(构建产物 / VCS / IDE)。 */
    private static final Set<String> SKIP_DIRS = Set.of(
            "target", "build", "out", "node_modules", ".git", ".idea", ".gradle", "bin", "dist");

    private final TagExtractor extractor = new TagExtractor();
    private final RepoMapRanker ranker = new RepoMapRanker();
    private final RepoMapRenderer renderer = new RepoMapRenderer();

    /**
     * @param repoRoot  仓库根
     * @param seedFiles diff 改动文件(相对正斜杠路径)—— 排名 personalization 种子
     * @return 签名级地图文本;无可定位定义时返回空串
     */
    public String build(Path repoRoot, Set<String> seedFiles) {
        List<Tag> all = new ArrayList<>();
        for (Path file : scanSourceFiles(repoRoot)) {
            String rel = repoRoot.relativize(file).toString().replace('\\', '/');
            try {
                if (Files.size(file) > MAX_FILE_BYTES) continue;
                all.addAll(extractor.extract(rel, Files.readString(file, StandardCharsets.UTF_8)));
            } catch (IOException e) {
                // 单文件读失败跳过,不中断建图。
            }
        }
        List<Tag> ranked = ranker.rank(all, seedFiles);
        return renderer.render(ranked);
    }

    /** 扫描仓库内的 .java 文件(跳过构建/VCS 目录),按路径排序保证确定性,限上限。 */
    private List<Path> scanSourceFiles(Path repoRoot) {
        List<Path> files = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(repoRoot)) {
            walk.filter(Files::isRegularFile)
                    .filter(p -> p.getFileName().toString().endsWith(".java"))
                    .filter(p -> !isUnderSkippedDir(repoRoot, p))
                    .sorted()
                    .limit(MAX_FILES)
                    .forEach(files::add);
        } catch (IOException e) {
            // 扫描失败返回已收集到的部分(可能为空),上层渲染空地图。
        }
        return files;
    }

    private static boolean isUnderSkippedDir(Path repoRoot, Path file) {
        Path rel = repoRoot.relativize(file);
        for (Path seg : rel) {
            if (SKIP_DIRS.contains(seg.toString())) return true;
        }
        return false;
    }
}

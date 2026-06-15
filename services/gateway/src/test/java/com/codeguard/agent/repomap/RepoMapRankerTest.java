package com.codeguard.agent.repomap;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Group 2 工程正确性:PageRank 确定性 + ranker 的 diff 种子偏向、seed 定义剔除、排序确定。
 */
class RepoMapRankerTest {

    private final RepoMapRanker ranker = new RepoMapRanker();

    private int indexOfName(List<Tag> tags, String name) {
        for (int i = 0; i < tags.size(); i++) {
            if (tags.get(i).name().equals(name)) return i;
        }
        return -1;
    }

    @Test
    void pageRankIsDeterministicAndStructural() {
        Set<String> nodes = Set.of("a", "b", "c");
        List<PageRank.Edge> edges = List.of(
                new PageRank.Edge("a", "b", 1.0),
                new PageRank.Edge("c", "b", 2.0),
                new PageRank.Edge("a", "c", 1.0));

        // 均匀 personalization 下检验结构:b 被两条边(其一权重更高)指入,应 rank 最高。
        Map<String, Double> uniform1 = PageRank.compute(nodes, edges, Map.of());
        Map<String, Double> uniform2 = PageRank.compute(nodes, edges, Map.of());
        assertEquals(uniform1, uniform2, "同输入必同输出(确定性)");
        assertTrue(uniform1.get("b") > uniform1.get("a"));
        assertTrue(uniform1.get("b") > uniform1.get("c"));
    }

    @Test
    void personalizationBiasesRankTowardSeed() {
        Set<String> nodes = Set.of("a", "b", "c");
        List<PageRank.Edge> edges = List.of(
                new PageRank.Edge("a", "b", 1.0),
                new PageRank.Edge("c", "b", 2.0),
                new PageRank.Edge("a", "c", 1.0));

        double aUniform = PageRank.compute(nodes, edges, Map.of()).get("a");
        double aSeeded = PageRank.compute(nodes, edges, Map.of("a", 1.0)).get("a");
        assertTrue(aSeeded > aUniform, "把 personalization 放到 a 上应抬高 a 的 rank");
    }

    /** 构造:seed 文件引用 validate;非 seed 文件引用 helper。两者各被定义一次。 */
    private List<Tag> sampleTags() {
        return List.of(
                Tag.ref("Changed.java", "validate", 5),
                Tag.ref("Other.java", "helper", 9),
                Tag.def("Validator.java", "validate", 3, "boolean validate(String s)"),
                Tag.def("Validator.java", "Validator", 1, "class Validator"),
                Tag.def("Helper.java", "helper", 2, "String helper()"),
                Tag.def("Changed.java", "doWork", 4, "void doWork()") // seed 自身定义,应被剔除
        );
    }

    @Test
    void seedReferencedDefinitionOutranksOthers() {
        List<Tag> ranked = ranker.rank(sampleTags(), Set.of("Changed.java"));

        int vi = indexOfName(ranked, "validate");
        int hi = indexOfName(ranked, "helper");
        assertTrue(vi >= 0, "validate 应在地图中");
        assertTrue(hi >= 0, "helper 应在地图中");
        assertTrue(vi < hi, "被 diff 改动文件引用的 validate 应排在 helper 之前");
    }

    @Test
    void seedFileOwnDefinitionsExcluded() {
        List<Tag> ranked = ranker.rank(sampleTags(), Set.of("Changed.java"));
        assertFalse(ranked.stream().anyMatch(t -> t.relFile().equals("Changed.java")),
                "diff 改动文件自身的定义不应进地图");
    }

    @Test
    void rankingIsDeterministic() {
        List<Tag> r1 = ranker.rank(sampleTags(), Set.of("Changed.java"));
        List<Tag> r2 = ranker.rank(sampleTags(), Set.of("Changed.java"));
        assertEquals(r1, r2);
    }

    @Test
    void noEdgesYieldsEmpty() {
        // 只有定义、无引用 → 无边 → 空地图。
        List<Tag> onlyDefs = List.of(Tag.def("A.java", "foo", 1, "void foo()"));
        assertEquals(List.of(), ranker.rank(onlyDefs, Set.of()));
    }
}

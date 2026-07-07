package com.codeguard.agent.repomap;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 加权 personalized PageRank,自实现幂迭代(不引图库,见 design.md D4)。
 * <p>
 * 对齐 aider repo map 的做法:节点 = 文件,边 = 带权有向引用关系,personalization 向量把排名
 * 偏向"种子"(我们用 diff 改动文件)。确定性:同一输入(节点/边/种子/超参)必产出同一排名。
 * <p>
 * 标准公式(含 teleport 与 dangling 处理):
 * <pre>
 *   r' = (1-d)·p  +  d·(A r)  +  d·(dangling 质量)·p
 * </pre>
 * 其中 p 为归一化的 personalization 向量;dangling(无出边)节点的质量按 p 重新分配。
 */
public final class PageRank {

    /** 有向带权边。 */
    public record Edge(String src, String dst, double weight) {}

    public static final double DEFAULT_DAMPING = 0.85;
    public static final int DEFAULT_MAX_ITER = 100;
    public static final double DEFAULT_TOL = 1e-6;

    private PageRank() {}

    public static Map<String, Double> compute(Set<String> nodes,
                                              List<Edge> edges,
                                              Map<String, Double> personalization) {
        return compute(nodes, edges, personalization, DEFAULT_DAMPING, DEFAULT_MAX_ITER, DEFAULT_TOL);
    }

    public static Map<String, Double> compute(Set<String> nodes,
                                              List<Edge> edges,
                                              Map<String, Double> personalization,
                                              double damping, int maxIter, double tol) {
        int n = nodes.size();
        if (n == 0) {
            return Map.of();
        }

        // personalization 向量 p:归一化;为空或全 0 时退化为均匀分布。
        Map<String, Double> p = new HashMap<>();
        double persSum = 0.0;
        for (String node : nodes) {
            double v = personalization == null ? 0.0 : personalization.getOrDefault(node, 0.0);
            if (v < 0) v = 0;
            p.put(node, v);
            persSum += v;
        }
        if (persSum <= 0) {
            for (String node : nodes) p.put(node, 1.0 / n);
        } else {
            for (String node : nodes) p.put(node, p.get(node) / persSum);
        }

        // 出边权重和(用于把质量按权重分摊)。
        Map<String, Double> outWeight = new HashMap<>();
        for (Edge e : edges) {
            if (nodes.contains(e.src()) && nodes.contains(e.dst()) && e.weight() > 0) {
                outWeight.merge(e.src(), e.weight(), Double::sum);
            }
        }

        Map<String, Double> rank = new HashMap<>(p); // 以 personalization 起步

        for (int iter = 0; iter < maxIter; iter++) {
            Map<String, Double> next = new HashMap<>();
            // teleport 项 (1-d)·p
            for (String node : nodes) {
                next.put(node, (1.0 - damping) * p.get(node));
            }
            // dangling 质量:无出边节点的 rank 按 p 重新分配
            double dangling = 0.0;
            for (String node : nodes) {
                if (!outWeight.containsKey(node)) {
                    dangling += rank.get(node);
                }
            }
            if (dangling > 0) {
                for (String node : nodes) {
                    next.merge(node, damping * dangling * p.get(node), Double::sum);
                }
            }
            // 沿边传播 d·(A r)
            for (Edge e : edges) {
                if (e.weight() <= 0) continue;
                Double ow = outWeight.get(e.src());
                if (ow == null || ow <= 0) continue;
                if (!nodes.contains(e.dst())) continue;
                double contrib = damping * rank.get(e.src()) * e.weight() / ow;
                next.merge(e.dst(), contrib, Double::sum);
            }
            // 收敛判定(L1)
            double diff = 0.0;
            for (String node : nodes) {
                diff += Math.abs(next.get(node) - rank.get(node));
            }
            rank = next;
            if (diff < tol) break;
        }
        return rank;
    }
}

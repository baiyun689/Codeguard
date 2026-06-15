package com.codeguard.agent.repomap;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 把排好序的 DEF tag 渲染成**签名级**地图,受 token 预算约束。
 * <p>
 * 借鉴 aider repo map 的呈现(见 design.md D1):按文件分组,每个定义只输出**签名**,实现体以
 * {@code ⋮...} 占位省略 —— 用最少的 token 让审查员知道"有这些类/方法、长什么样、在哪个文件",
 * 需要实现细节再去 get_file_content 细读。
 * <p>
 * 预算裁剪:输入已按相关性降序,逐个纳入,累计 token 预估超出预算即停 —— 高排名优先保留,
 * 低排名被裁。token 预估用"字符数 / {@value #CHARS_PER_TOKEN}"的廉价近似(无需引 tokenizer)。
 */
public final class RepoMapRenderer {

    /** 默认 token 预算(对齐 aider 的 map-tokens 默认 1k)。 */
    public static final int DEFAULT_MAX_TOKENS = 1024;

    /** 粗略 token 估算:平均每 token 约 4 字符(英文/代码量级)。 */
    private static final int CHARS_PER_TOKEN = 4;

    private static final String ELISION = "    ⋮...";

    public String render(List<Tag> rankedDefs) {
        return render(rankedDefs, DEFAULT_MAX_TOKENS);
    }

    /**
     * @param rankedDefs 已按相关性降序的 DEF tag
     * @param maxTokens  token 预算
     * @return 签名级地图文本;无可渲染内容时返回空串
     */
    public String render(List<Tag> rankedDefs, int maxTokens) {
        if (rankedDefs == null || rankedDefs.isEmpty()) {
            return "";
        }
        int budgetChars = Math.max(0, maxTokens) * CHARS_PER_TOKEN;

        // 先按"纳入顺序"(= 相关性)挑出预算内的定义,再按文件分组渲染,保证高排名定义优先入选。
        // 用 LinkedHashMap 保持文件首次出现顺序,组内保持相关性顺序。
        Map<String, List<Tag>> byFile = new java.util.LinkedHashMap<>();
        int usedChars = 0;
        for (Tag def : rankedDefs) {
            if (def.kind() != Tag.Kind.DEF || def.signature() == null) continue;
            // 估算这条签名渲染后的增量(签名 + 缩进 + 换行 + 可能的文件头)。
            int lineCost = def.signature().length() + 8;
            if (!byFile.containsKey(def.relFile())) {
                lineCost += def.relFile().length() + 2; // 新文件头
            }
            if (usedChars + lineCost > budgetChars && !byFile.isEmpty()) {
                break; // 预算用尽:已纳入高排名项,停。
            }
            byFile.computeIfAbsent(def.relFile(), k -> new ArrayList<>()).add(def);
            usedChars += lineCost;
        }

        if (byFile.isEmpty()) {
            return "";
        }

        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, List<Tag>> entry : byFile.entrySet()) {
            sb.append(entry.getKey()).append(":\n");
            sb.append(ELISION).append('\n');
            for (Tag def : entry.getValue()) {
                sb.append("│ ").append(def.signature()).append('\n');
                sb.append(ELISION).append('\n');
            }
            sb.append('\n');
        }
        return sb.toString().stripTrailing() + "\n";
    }
}

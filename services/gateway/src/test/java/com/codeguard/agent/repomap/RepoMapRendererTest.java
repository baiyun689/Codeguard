package com.codeguard.agent.repomap;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Group 3:渲染器只出签名不出实现;受 token 预算裁剪、高排名优先保留。
 */
class RepoMapRendererTest {

    private final RepoMapRenderer renderer = new RepoMapRenderer();

    @Test
    void rendersSignatureWithElisionNoBody() {
        List<Tag> defs = List.of(
                Tag.def("Validator.java", "validate", 3, "boolean validate(String s)"));
        String out = renderer.render(defs);

        assertTrue(out.contains("Validator.java:"), "含文件头");
        assertTrue(out.contains("boolean validate(String s)"), "含签名");
        assertTrue(out.contains("⋮"), "含省略占位");
        assertFalse(out.contains("return"), "不含实现体");
    }

    @Test
    void emptyInputRendersEmpty() {
        assertEquals("", renderer.render(List.of()));
    }

    @Test
    void budgetKeepsHighRankedDropsLowRanked() {
        // 造 50 条定义,排名靠前的是 first,靠后的是 last_*。极小预算只容得下最前面的。
        List<Tag> defs = new ArrayList<>();
        defs.add(Tag.def("First.java", "topRanked", 1, "void topRanked()"));
        for (int i = 0; i < 50; i++) {
            defs.add(Tag.def("Low" + i + ".java", "low" + i, 1, "void low" + i + "()"));
        }
        String out = renderer.render(defs, 8); // 8 tokens ≈ 32 字符:只够最前面一条

        assertTrue(out.contains("topRanked"), "高排名保留");
        assertFalse(out.contains("low49"), "低排名被裁");
    }
}

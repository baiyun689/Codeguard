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

    // --- 调用方段(change repomap-include-callers) ---

    @Test
    void callersSurviveNeighborhoodBudgetExhaustion() {
        // 邻域很多、主预算极小(被裁),但调用方有独立保留预算,仍应呈现。
        List<Tag> neighborhood = new ArrayList<>();
        for (int i = 0; i < 50; i++) {
            neighborhood.add(Tag.def("N" + i + ".java", "n" + i, 1, "void n" + i + "()"));
        }
        List<Tag> callers = List.of(
                Tag.def("GreetingService.java", "GreetingService", 1, "class GreetingService"),
                Tag.def("GreetingService.java", "greet", 5, "String greet(String id)"));

        String out = renderer.render(neighborhood, callers, 4, 256, 10); // 邻域 4 tokens 极小
        assertTrue(out.contains("直接调用方"), "含调用方段标题");
        assertTrue(out.contains("GreetingService.java:"), "调用方文件未被邻域预算挤出");
        assertTrue(out.contains("String greet(String id)"), "含调用方方法签名");
    }

    @Test
    void callersTruncatedAtMaxWithRemainderNote() {
        List<Tag> callers = new ArrayList<>();
        for (int i = 0; i < 12; i++) {
            String name = String.format("Caller%02d", i);
            callers.add(Tag.def(name + ".java", name, 1, "class " + name));
        }
        String out = renderer.render(List.of(), callers, 1024, 256, 10);
        assertTrue(out.contains("Caller00"), "前 10 个调用方应列出");
        assertTrue(out.contains("Caller09"), "第 10 个调用方应列出");
        assertFalse(out.contains("Caller10"), "超上限的调用方应被截断");
        assertTrue(out.contains("+2"), "应标注其余调用方数量");
    }

    @Test
    void emptyCallersProducesNoCallerSection() {
        List<Tag> neighborhood = List.of(Tag.def("V.java", "v", 1, "void v()"));
        String out = renderer.render(neighborhood, List.of());
        assertTrue(out.contains("V.java:"), "邻域照常渲染");
        assertFalse(out.contains("直接调用方"), "无调用方则不输出调用方段");
    }
}

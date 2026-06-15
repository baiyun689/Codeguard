package com.codeguard.agent.repomap;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * TagExtractor 工程正确性:抽出预期的 def/ref;语法错误文件被跳过(返回空、不抛)。
 */
class TagExtractorTest {

    private final TagExtractor extractor = new TagExtractor();

    private Set<String> namesOf(List<Tag> tags, Tag.Kind kind) {
        return tags.stream().filter(t -> t.kind() == kind).map(Tag::name).collect(Collectors.toSet());
    }

    @Test
    void extractsDefsAndRefs() {
        String src = """
                package demo;
                class Service {
                    private String token;
                    int add(int a, int b) {
                        Helper h = new Helper();
                        return h.combine(a, b);
                    }
                }
                """;
        List<Tag> tags = extractor.extract("demo/Service.java", src);

        Set<String> defs = namesOf(tags, Tag.Kind.DEF);
        assertTrue(defs.contains("Service"), "类定义");
        assertTrue(defs.contains("add"), "方法定义");
        assertTrue(defs.contains("token"), "字段定义");

        Set<String> refs = namesOf(tags, Tag.Kind.REF);
        assertTrue(refs.contains("combine"), "方法调用引用");
        assertTrue(refs.contains("Helper"), "类型/new 引用");
    }

    @Test
    void methodDefCarriesSignatureWithoutBody() {
        String src = "class C { int add(int a, int b) { return a + b; } }";
        Tag addDef = extractor.extract("C.java", src).stream()
                .filter(t -> t.kind() == Tag.Kind.DEF && t.name().equals("add"))
                .findFirst().orElseThrow();

        assertTrue(addDef.signature().contains("add(int a, int b)"));
        assertFalse(addDef.signature().contains("return"), "签名不应含方法体");
    }

    @Test
    void unparseableFileYieldsEmptyListNotThrow() {
        List<Tag> tags = extractor.extract("Broken.java", "class { this is not valid java @@@");
        assertEquals(List.of(), tags);
    }
}

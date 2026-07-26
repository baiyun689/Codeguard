package com.codeguard.agent.repomap;

import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 按**文件扩展名**把源文件路由到对应的 {@link TagExtractor} —— repo map 多语言适配的接缝。
 * <p>
 * {@link RepoMapBuilder} 用它做两件事:① 扫描时只收 {@link #supports 受支持扩展名}的文件;
 * ② 对每个文件用 {@link #forFile} 选抽取器。新增一门语言只改这里(注册一行),建图/排名/渲染不动。
 * <p>
 * 扩展名一律小写比较(忽略大小写);MVP 仅注册 Java(见 design.md D3)。
 */
public final class TagExtractorRegistry {

    private final Map<String, TagExtractor> byExtension = new HashMap<>();

    /** MVP 默认:仅 Java。未来加语言在此追加注册。 */
    public static TagExtractorRegistry defaults() {
        TagExtractorRegistry registry = new TagExtractorRegistry();
        registry.register("java", new JavaTagExtractor());
        return registry;
    }

    /** 注册某扩展名(不含点,如 {@code "java"})的抽取器;同名覆盖。 */
    public void register(String extension, TagExtractor extractor) {
        byExtension.put(extension.toLowerCase(Locale.ROOT), extractor);
    }

    /** 受支持的扩展名集合(小写,不含点)—— 供扫描阶段做文件过滤。 */
    public Set<String> supportedExtensions() {
        return Set.copyOf(byExtension.keySet());
    }

    /** 该文件名的扩展名是否有对应抽取器。 */
    public boolean supports(String fileName) {
        return byExtension.containsKey(extensionOf(fileName));
    }

    /** 返回该文件名对应的抽取器;无则返回 {@code null}。 */
    public TagExtractor forFile(String fileName) {
        return byExtension.get(extensionOf(fileName));
    }

    /** 取文件名的小写扩展名(不含点);无扩展名返回空串。 */
    private static String extensionOf(String fileName) {
        int dot = fileName.lastIndexOf('.');
        if (dot < 0 || dot == fileName.length() - 1) {
            return "";
        }
        return fileName.substring(dot + 1).toLowerCase(Locale.ROOT);
    }
}

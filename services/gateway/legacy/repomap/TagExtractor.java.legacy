package com.codeguard.agent.repomap;

import java.util.List;

/**
 * 把单个源文件抽成符号 tag(def/ref)的抽取器 —— repo map 中**唯一与语言相关**的环节。
 * <p>
 * 下游(Ranker / PageRank / Renderer)只认 {@link Tag},完全语言无关;新增一门语言 =
 * 实现一个 {@code TagExtractor} 并在 {@link TagExtractorRegistry} 按扩展名注册即可,
 * 建图/排名/渲染一行不用动(见 design.md D3:借鉴 aider 的算法,抽取栈可换)。
 * <p>
 * 约定:确定性纯函数(同一源码必产出同一组 tag);无法解析的源码返回**空列表、不抛异常**
 * —— 审查仓库混入语法错误/非标准文件是常态,不能让一处坏文件拖垮整次建图。
 */
public interface TagExtractor {

    /**
     * @param relFile 相对仓库根的正斜杠路径(仅用于回填到 {@link Tag},不读盘)
     * @param source  源文件内容
     * @return 抽出的 tag;不可解析时返回空列表
     */
    List<Tag> extract(String relFile, String source);
}

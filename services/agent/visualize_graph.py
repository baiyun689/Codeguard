"""生成 ADR-032 ReviewCouncil LangGraph 编排图的可视化输出。

用法:
    cd services/agent
    conda run -n codeguard python visualize_graph.py          # 打印 ASCII + 输出 mermaid
    conda run -n codeguard python visualize_graph.py --png    # 额外生成 PNG
    conda run -n codeguard python visualize_graph.py --all    # ASCII + Mermaid + PNG + JSON
"""

from __future__ import annotations

import argparse
import sys
import os

# 确保项目 src 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from codeguard_agent.pipeline.graph import build_review_graph


def main():
    parser = argparse.ArgumentParser(description="可视化 ReviewCouncil LangGraph 编排图")
    parser.add_argument("--png", action="store_true", help="生成 PNG 图片")
    parser.add_argument("--all", action="store_true", help="生成所有格式 (ASCII + Mermaid + PNG + JSON)")
    parser.add_argument("--summary-off", action="store_true", help="模拟 enable_summary=False 的拓扑")
    parser.add_argument("-o", "--output", default="council_graph", help="输出文件前缀 (默认 council_graph)")
    args = parser.parse_args()

    enable_summary = not args.summary_off

    print("=" * 70)
    print(f"  Codeguard ReviewCouncil LangGraph 编排图")
    print(f"  enable_summary={enable_summary}")
    print("=" * 70)

    # 编译图 (不需要 LLM / tool_client，纯结构可视化)
    compiled = build_review_graph(
        enable_summary=enable_summary,
        checkpointer=None,
        llm=None,
        fp_verify_llm=None,
        tool_client=None,
    )

    graph = compiled.get_graph()

    # ── 1. ASCII 图 ──
    print("\n📐 ASCII 拓扑图:\n")
    print(graph.draw_ascii())

    # ── 2. Mermaid ──
    mermaid_text = graph.draw_mermaid()
    mermaid_path = f"{args.output}.mermaid"
    with open(mermaid_path, "w", encoding="utf-8") as f:
        f.write(mermaid_text)
    print(f"✅ Mermaid 图已保存: {mermaid_path}")
    print("   可在 https://mermaid.live 粘贴查看，或在 VS Code 中安装 Mermaid 插件预览")

    # ── 3. PNG (可选) ──
    if args.png or args.all:
        try:
            png_data = graph.draw_mermaid_png()
            png_path = f"{args.output}.png"
            with open(png_path, "wb") as f:
                f.write(png_data)
            print(f"✅ PNG 图片已保存: {png_path}")
        except Exception as e:
            print(f"⚠️  PNG 生成失败: {e}")
            print("   提示: 需要安装 extra 依赖: pip install 'langgraph[image]'")

    # ── 4. JSON ──
    if args.all:
        import json
        json_path = f"{args.output}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(graph.to_json(), f, ensure_ascii=False, indent=2)
        print(f"✅ JSON 结构已保存: {json_path}")

    # ── 5. 打印节点信息 ──
    print(f"\n📊 图统计:")
    print(f"   节点数: {len(graph.nodes)}")
    print(f"   边数: {len(graph.edges)}")
    print(f"\n   节点列表:")
    for node_id, node_data in graph.nodes.items():
        print(f"     • {node_id}")

    print(f"\n   边列表:")
    for edge in graph.edges:
        src = edge.source
        tgt = edge.target
        cond = f" [条件: {edge.conditional}]" if edge.conditional else ""
        print(f"     • {src} → {tgt}{cond}")


if __name__ == "__main__":
    main()

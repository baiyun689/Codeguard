"""工具调用支持(阶段 3)。

Python 智能层经 HTTP 调用 Java 护栏层提供的工具(见 openspec design.md D0 职责边界)。
本包只负责"客户端 + 工具定义";工具的实际执行与安全护栏都在 Java 侧。
"""

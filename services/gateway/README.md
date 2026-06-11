# Java Gateway(占位)

阶段 3 才引入。届时这里会是一个 Java 服务,负责为 Python Agent 提供
重型的代码分析工具(通过 HTTP 暴露):

- `get_file_content` —— 读取项目源码
- `get_method_definition` —— 基于 AST(JavaParser)提取方法定义
- `get_call_graph` —— 遍历代码调用图
- `semantic_search` —— 向量检索(RAG)

当前阶段(1–2)只有 Python Agent,这里保持空占位,提醒"双语言架构留有位置"。

# Codeguard Gateway(Java · 护栏 + 地面真值层)

阶段 3 引入的 Java 侧。职责边界(见根目录 `DECISIONS.md` ADR-009 / openspec design.md D0):
**只做"事实与护栏"——安全沙箱、代码地面真值、重静态计算;绝不调 LLM、不判断"是不是问题"**(那是 Python 智能层的事)。

## 当前能力(阶段 3 第一步)

通过 HTTP 为 Python Agent 提供工具回调,会话化、通用分发:

- `POST /api/v1/tools/session` —— 创建会话(repo 路径 + 本次 diff 改动文件集合)→ `session_id`
- `DELETE /api/v1/tools/session/{id}` —— 销毁会话
- `POST /api/v1/tools/{name}` —— 通用工具分发(需 `X-Session-Id`),本期已注册:
  - `get_file_content` —— 读取仓库内文件,受 `FileAccessSandbox` 护栏(防穿越 + 限 diff 范围 + 大小上限)
- `GET /health` —— 健康检查

## 跑起来

```bash
mvn package                                 # 跑单测 + 出 fat jar
java -jar target/codeguard-gateway.jar      # 默认端口 9090(CODEGUARD_TOOL_SERVER_PORT 可覆盖)
```

Python 侧设 `CODEGUARD_TOOL_SERVER_URL=http://localhost:9090` 后,`--mode pipeline` 的审查员即走 ReAct,可调上述工具。

## 后续(逐个加,沿通用协议 + 会话接缝)

- `get_method_definition`(JavaParser AST)、`get_call_graph`(调用图)、`semantic_search`(向量 RAG)、`search_memory`(记忆,阶段 4)。
- 重资源(调用图/向量索引)届时按 project 在 `ToolSessionManager` 预留的挂载点共享。

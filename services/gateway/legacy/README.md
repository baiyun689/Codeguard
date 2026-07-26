# Gateway Legacy Archive

This directory contains historical Java Gateway implementations that are kept
for reference only. Maven does not compile or package anything below
`services/gateway/legacy`.

Archived Java sources use the `.java.legacy` suffix. This preserves their
contents while preventing the project snapshot scanner, Maven, and IDE source
discovery from treating duplicate historical classes as active code.

## Contents

- `pre-modular-gateway/`: the former root `services/gateway/src` tree from
  before the Gateway was split into `shared`, `tool-server`, `ci-webhook`, and
  `llm-proxy` modules.
- `pre-codegraph-tool-server/`: the old per-request AST scanners and their
  tests (`get_diff_ast`, simple-name `find_callers`, sensitive API scanning,
  and file metrics) that were replaced by `ProjectSnapshot` /
  `ProjectCodeGraph`.
- `repomap/`, `tools/`, and `test/`: the earlier repo-map implementation.

The legacy tool names remain available at runtime through
`GraphCompatibilityTool`; that adapter queries the current project graph and
is not part of this archive.

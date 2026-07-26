# Agent Legacy Archive

This directory contains historical Agent orchestration implementations and
tests. It is outside `src/codeguard_agent`, is not included in the wheel, and
is not collected by the default pytest configuration.

- `supervisor_graph/`: the retired supervisor-driven reviewer loop.
- `runtime_archive/codeguard_agent/legacy/`: packaged legacy stages, prompts,
  and false-positive rules removed after `CouncilJudge` became the only final
  decision path.
- `runtime_archive/config/`: configuration used only by the archived
  false-positive rules.
- `tests/`: historical tests for the archived stages.

The old Gateway protocol names are not archived here. They remain active
compatibility adapters for eval profiles and evidence capability mapping.

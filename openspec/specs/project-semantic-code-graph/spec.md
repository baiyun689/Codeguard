# Project Semantic Code Graph

## Requirements

### Exact immutable project snapshot

The Tool Server SHALL build one immutable snapshot per exact project revision and SHALL retain all reviewable Java source text, complete parsed ASTs, semantic symbols, relationships, and parse diagnostics. Concurrent sessions using the same key SHALL share one build.

### Grounded reviewer navigation

ContextProvider SHALL resolve changed lines to stable symbol identifiers before reviewer execution. Reviewer graph tools SHALL accept grounded symbol identifiers and SHALL return bounded relationships with provenance, coverage, and limitations. File reads outside the current task SHALL be accepted only when the snapshot confirms the path.

### Explicit uncertainty

Graph queries SHALL distinguish confirmed facts, complete-coverage absence, and unknown or partial coverage. Unknown, partial, timeout, ambiguous, reflection, generated-code, and unresolved dependency cases SHALL NOT be represented as confirmed absence.

### Role-oriented tools

Threat modeling SHALL use `inspect_security_path`, behavior analysis SHALL use `inspect_change_impact`, and maintainability analysis SHALL use `inspect_structure`. The legacy AST, caller, sensitive API, and metric names SHALL remain temporary adapters over the same snapshot implementation.

### Evidence capability mapping

Evidence planning SHALL declare semantic evidence capabilities rather than construct file-and-method guesses. Evidence execution SHALL map those capabilities to graph tools, deduplicate equivalent queries, and downgrade unknown graph results to insufficient evidence.

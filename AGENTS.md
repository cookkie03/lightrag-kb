# Project instructions

## Code retrieval

Use Graphify to retrieve relevant parts of the codebase whenever a user raises a question about the project, before investigating or answering. Verify relevant details against the source: the graph may be incomplete or stale. If Graphify is unavailable, say so and use direct code search instead.

## Comments

Use Redis-style comments to explain the purpose of functions, classes, decorators and complex loops, including invariants and rationale.

- Place a concise comment immediately before each function, class and decorator definition; avoid merely repeating its name.
- Explain why a decision was made, especially for security, data integrity, constraints and non-obvious edge cases. Do not narrate obvious statements.
- Document preconditions, side effects, errors and result guarantees when important and not evident from the signature.
- For complex loops, explain the invariant and any important ordering constraints.
- Write complete sentences in English. In Python, use `#` comments; use docstrings when documentation must be available through `help()` or other tooling.
- Prefer clear names and simple code to explanatory comments. Update affected comments when behavior changes, and remove stale or redundant comments.

Prefer simple, minimal Python that a beginner can understand. Favor explicit, straightforward code over advanced language features or clever abstractions, without sacrificing security, validation, error handling or data integrity.

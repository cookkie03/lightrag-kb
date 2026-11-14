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

## Python style

Prefer simple, minimal Python that a beginner can understand. Favor explicit, straightforward code over advanced language features or clever abstractions, without sacrificing security, validation, error handling or data integrity.

- Use plain `dict` and helper functions instead of `@dataclass` or custom classes when a simple record is enough.
- Write procedural code with explicit `try/finally` blocks instead of generators, decorators, or `contextmanager`.
- Use plain `for` loops with `if/else` instead of nested comprehensions, `any()`, `all()`, or lambdas.
- Use `sys.argv` or basic `argparse` instead of complex CLI frameworks.
- Keep one shared Python runtime for CLI, server, and parsers. No external databases (no SQLite, PostgreSQL, Neo4j, Redis server). Persist data only in local JSON files under `kb/<kb-id>/`.

## Non-negotiable safety requirements

Even in the simplest possible code, these must never be removed or weakened:

- **Atomic file writes**: use `fcntl.flock` for locking, `tempfile.mkstemp` for a temporary file, and `os.replace` for atomic replacement. This prevents JSON corruption on crash or concurrent access.
- **Path validation**: always resolve paths with `os.path.abspath` or `Path.resolve` before reading or writing, to block path traversal attacks.
- **Redis-style comments**: every critical block must document in English why the invariant exists. Never leave security or data integrity logic uncommented.

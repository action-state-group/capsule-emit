# Staged partner-page draft: agno-agi/agno

`observability-capsule-emit.mdx` in this directory is a **staged draft** of the
capsule-emit observability page for the upstream **agno-agi/agno** docs. It is
authored here so it is neutrality-gated, durable, and liftable — it is **not** a
capsule-emit doc and should not be treated as one.

- **Destination (upstream):** `docs/.../observability/capsule-emit.mdx` in
  `agno-agi/agno`, mirroring the CrewAI precedent
  ([crewAIInc/crewAI#7113](https://github.com/crewAIInc/crewAI/pull/7113)).
- **Scope:** the **English** page body only. Localization (ar/ko/pt-BR),
  `overview.mdx` card, and `docs.json` nav wiring are the field engineer's to do
  upstream.
- **Status:** awaiting the framework-docs ruling and the FDE upstream post.
  **Do not** post to the external repo from here — posting is Jody-dispatch.

The page uses the real `capsule-emit[agno]==0.5.1` API only:
`AgnoCapsuleListener` registered once via Agno's `tool_hooks`, `operator=` /
`developer=` config, the `[agno]` extra (pins `agno>=3.0.0`), and
`agent-action-capsule verify --store` for the verify step. Every symbol was
verified against the installed 0.5.1 wheel.

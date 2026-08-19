"""The Escalator — drafts the DFS complaint on a clock.breach event.

Reads the claim and its breach record from Firestore, drafts a complaint via
Gemini 3.5 Flash (the only LLM call anywhere in this agent), writes an
`escalations` document exactly once per claim, and marks the claim
escalated. Never auto-files — CLAUDE.md Rule 3: the agent assembles and
escalates, it never asserts a valuation, and filing stays one click from a
human, not automatic.
"""

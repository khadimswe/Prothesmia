"""The Clock — the agent that never sleeps.

Reads open claims, evaluates each against `statutes.rules` (never an LLM),
writes an append-only `clock_checks` row per claim per day, and escalates a
breach to Pub/Sub `clock.breach` exactly once per claim.
"""

"""Capture adapters — each turns some capture source into a SessionTranscript.

Adapter #1 (this POC): `codex_rollout` reads the Codex CLI's own session
rollout logs. Adapter #2 (production): a praxis gateway sampling-tap emitting
events. Both produce the same SessionTranscript, so everything downstream is
capture-agnostic.
"""

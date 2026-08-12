"""Semantic memory layer — distilled artifact summaries + lineage.

Cerebras-inspired: state lives in files/documents, not conversation. Each
hop's artifacts are distilled (HANDOFF.md) and their summaries + lineage are
durably recorded so the next hop retrieves *minimum viable context* instead
of raw history. Backends: local JSONL (default/offline) or Firestore (cloud).
"""

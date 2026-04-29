"""Feishu output-side presentation layer (spec 005).

Submodules land incrementally:
- ``cards.v2``      — JSON 2.0 card-building helpers (M0-A, this PR series)
- ``events``        — OutputEvent data model (M1-A)
- ``kinds``         — MessageKind enum (M1-A)
- ``envelope``      — MessageEnvelope (M1-A)
- ``messages.*``    — LeafFormatter implementations (M1-B+)
- ``composer``      — FeishuMessageComposer orchestrator (M1-E)

During M0 only ``cards.v2`` is populated; everything else arrives in M1.
"""

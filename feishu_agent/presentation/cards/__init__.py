"""Feishu card construction helpers.

Only JSON 2.0 helpers (:mod:`.v2`) are provided; we deliberately do not
support JSON 1.0 since the composer (spec 005) targets 2.0 exclusively.

See :mod:`feishu_agent.presentation.cards.v2` for the full set of
helper functions and the design constraints they enforce.
"""

from feishu_agent.presentation.cards import v2

__all__ = ["v2"]

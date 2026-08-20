"""Adapters joining the generic ERP pipeline to specific existing systems.

Deliberately a SEPARATE TOP-LEVEL PACKAGE, not a sub-package of
``erp_pipeline``.

Phase 1 established, and a frozen test enforces, that nothing anywhere under
``erp_pipeline`` imports the ``bpi2020`` prototype - the framework must not
depend on the source-specific prototype it will eventually replace. The
Phase 10 cascade repair nonetheless needs something that can see both sides.

That something lives here. It may import both, and it speaks to the pipeline
only through the generic Phase 10 protocols, so the sync coordinator never
learns that BPI exists.
"""

from __future__ import annotations

__all__: list[str] = []

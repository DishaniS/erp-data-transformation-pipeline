"""``python -m erp_pipeline.api`` - start the API with production wiring.

Delegates to the runtime composition root, so the module-scoped API package
still knows nothing about which database or vector store backs it.
"""

from __future__ import annotations

import sys

from erp_pipeline.runtime.application import run

if __name__ == "__main__":
    sys.exit(run())

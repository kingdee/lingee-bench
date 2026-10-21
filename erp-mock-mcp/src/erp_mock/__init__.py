"""ERP-Mock-MCP: a business-agnostic, deterministic ERP mock served over MCP.

Business content lives in declarative business packs; this package only holds the
engine. It must not import its subpackages so that every layer can read the version
constants below without pulling in other layers.
"""

from typing import Final

__all__ = ["FRAMEWORK_VERSION", "META_MODEL_VERSION"]

# Semantic version of the framework; keep equal to [project].version in pyproject.toml.
FRAMEWORK_VERSION: Final = "0.1.0"

# Semantic version of meta model v0, matched against requires_meta_model of each pack.
META_MODEL_VERSION: Final = "0.1.0"

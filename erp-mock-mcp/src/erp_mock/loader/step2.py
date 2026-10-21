"""Loading step 2: the meta model version every business pack requires.

The second step of design.md 2.3. A manifest declares the meta model its configuration is
written against, and the engine implements exactly one version of the meta model
(:data:`~erp_mock.META_MODEL_VERSION`, meta model v0, whose major version is 0), so a pack
written against another one is rejected at startup instead of failing later on a
configuration item the engine does not know (requirement 2.5).

``requires_meta_model`` is a version range (requirement 2.4,
:class:`~erp_mock.common.versions.VersionRange`): one or more comparators separated by
spaces, each of ``>=``, ``>``, ``<=``, ``<`` and ``=`` immediately followed by a concrete
version, a version written alone meaning ``=``; a version matches the range when it
satisfies every comparator, so ``>=0.1.0 <1.0.0`` matches meta model 0.1.0 while
``>=1.0.0 <2.0.0`` does not. Both a text that is not a range and a range the current
version does not match are reported at ``requires_meta_model`` of that pack's pack.yaml.

Every pack of :attr:`~erp_mock.loader.loader.Loader.packs` is checked, so one run reports
the packs in error together (requirement 2.2). The step leaves nothing behind: what it
settles is that the configuration files step 4 reads are written against the meta model
this engine implements.
"""

from collections.abc import Sequence
from typing import Final

from erp_mock import META_MODEL_VERSION
from erp_mock.common.versions import SemVer, VersionError, VersionRange
from erp_mock.loader.errors import StepErrors
from erp_mock.loader.step1 import PACK_FILE, PackSource

__all__ = ["CONFIG_PATH", "check"]

# The manifest item this step checks, and the ``config_path`` of every error it reports.
CONFIG_PATH: Final = "requires_meta_model"

# The meta model version the engine implements, the one every range is matched against.
_CURRENT: Final = SemVer.parse(META_MODEL_VERSION)


def check(packs: Sequence[PackSource], errors: StepErrors) -> None:
    """Run step 2 on ``packs``, recording what it finds.

    The step passed when ``errors`` stayed empty.
    """
    for pack in packs:
        text = pack.manifest.requires_meta_model
        try:
            required = VersionRange.parse(text)
        except VersionError as error:
            reason = f"不是合法的版本范围：{error.reason}"
            errors.add_at(pack.name, PACK_FILE, CONFIG_PATH, reason)
            continue
        if not required.matches(_CURRENT):
            # The range as the manifest writes it, not its canonical text, so the reason
            # quotes what the deployer reads in the file.
            reason = f"要求的元模型版本范围 {text} 不匹配当前元模型版本 {_CURRENT}"
            errors.add_at(pack.name, PACK_FILE, CONFIG_PATH, reason)

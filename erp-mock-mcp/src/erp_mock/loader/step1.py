"""Loading step 1: instance.yaml and the manifest of every pack it references.

The first step of design.md 2.3. It reads the instance configuration and settles which
business packs take part in the validation at all (requirement 2.1):

- ``<instance dir>/instance.yaml`` must exist, be readable as UTF-8 YAML (design.md 2.1)
  and validate against :class:`~erp_mock.config.instance.InstanceDef`. Any of these
  failing is an instance configuration error, and step 1 then checks no business pack:
  which packs there are is what instance.yaml says (requirement 2.12). The clock belongs
  to that model, so a ``mode`` outside today and fixed, a mode fixed without
  ``anchor_date``, a date the calendar does not have and a ``timezone`` outside the IANA
  database are reported at their config path below ``clock`` (requirement 16.3), as is a
  pack pinned to anything but a concrete version, which
  :data:`~erp_mock.config.pack.VersionText` rejects at ``packs.<pack>``
  (requirement 2.3).
- every pack of ``packs`` must have a directory of its name below ``--packs-dir``, that
  directory a ``pack.yaml`` that parses and validates against
  :class:`~erp_mock.config.pack.PackManifest`, and that manifest the referenced name and
  version (requirement 2.3). ``primitives`` is a list of the M0 primitives
  (:data:`~erp_mock.config.pack.PRIMITIVES`), so a manifest that declares any other one
  fails the model (requirement 2.6).

Every reference is checked, so one run reports the errors of all of them
(requirement 2.2). A reference whose directory, pack.yaml or manifest is in error is left
out of :class:`Step1Result`; the steps that would read it do not run anyway. What the step
leaves behind is the instance configuration, which step 4 validates the profiles and
datasets of, and one :class:`PackSource` per pack, whose manifest step 2 checks the
required meta model version of, step 3 the dependencies of, and whose directory step 4
reads the configuration files from.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from erp_mock.config.instance import InstanceDef
from erp_mock.config.pack import PackManifest
from erp_mock.loader.errors import StepErrors
from erp_mock.loader.yaml_io import YamlLoadError, load_yaml

__all__ = ["INSTANCE_FILE", "PACK_FILE", "PackSource", "Step1Result", "check"]

# The instance configuration, relative to the instance directory, and the manifest of a
# business pack, relative to the pack directory: the two files this step reads, and the
# ``file`` of every error it reports.
INSTANCE_FILE: Final = "instance.yaml"
PACK_FILE: Final = "pack.yaml"


@dataclass(frozen=True, slots=True)
class PackSource:
    """One business pack of instance.yaml whose manifest step 1 read.

    - ``name`` and ``version``: the pack as instance.yaml references it; the manifest
      declares the same two values, which is what step 1 checked.
    - ``directory``: ``<packs dir>/<name>``, where step 4 reads the configuration files.
    - ``manifest``: the validated pack.yaml.
    """

    name: str
    version: str
    directory: Path
    manifest: PackManifest


@dataclass(frozen=True, slots=True)
class Step1Result:
    """What step 1 leaves for the steps after it.

    ``packs`` are in the order of instance.yaml and hold the references step 1 accepted,
    so the tuple is short of a pack only when the step also reported an error.
    """

    instance: InstanceDef
    packs: tuple[PackSource, ...]


def check(instance_dir: Path, packs_dir: Path, errors: StepErrors) -> Step1Result | None:
    """Run step 1 on ``instance_dir`` and ``packs_dir``, recording what it finds.

    Returns the instance configuration together with the packs whose manifest was read,
    or None when instance.yaml itself is in error, in which case no pack is checked
    (requirement 2.12). The step passed only when ``errors`` stayed empty.
    """
    instance = _read_instance(instance_dir, errors)
    if instance is None:
        return None
    found: list[PackSource] = []
    for name, version in instance.packs.items():
        source = _read_pack(packs_dir / name, name, version, errors)
        if source is not None:
            found.append(source)
    return Step1Result(instance=instance, packs=tuple(found))


def _read_instance(instance_dir: Path, errors: StepErrors) -> InstanceDef | None:
    """Read and validate ``<instance_dir>/instance.yaml`` (requirements 2.12 and 16.3)."""
    if not instance_dir.is_dir():
        errors.add_at(None, INSTANCE_FILE, None, "--instance 指定的目录不存在")
        return None
    path = instance_dir / INSTANCE_FILE
    if not path.exists():
        errors.add_at(None, INSTANCE_FILE, None, "--instance 指定的目录中没有 instance.yaml")
        return None
    return _read(InstanceDef, path, None, INSTANCE_FILE, errors)


def _read_pack(directory: Path, name: str, version: str, errors: StepErrors) -> PackSource | None:
    """Read and validate the manifest of the pack ``name`` ``version`` (requirement 2.3).

    The reference itself, a directory that is not there, belongs to instance.yaml; what
    the directory holds is reported as an error of the pack.
    """
    if not directory.is_dir():
        reason = f"--packs-dir 下没有业务包 {name} 的目录（引用版本 {version}）"
        errors.add_at(None, INSTANCE_FILE, f"packs.{name}", reason)
        return None
    path = directory / PACK_FILE
    if not path.exists():
        reason = f"业务包目录中没有 pack.yaml（引用版本 {version}）"
        errors.add_at(name, PACK_FILE, None, reason)
        return None
    manifest = _read(PackManifest, path, name, PACK_FILE, errors)
    if manifest is None:
        return None
    # Both are reported, so a manifest that matches neither gives both errors at once.
    mismatched = False
    if manifest.pack != name:
        reason = f"pack 为 {manifest.pack}，与 instance.yaml 引用的包名 {name} 不同"
        errors.add_at(name, PACK_FILE, "pack", reason)
        mismatched = True
    if manifest.version != version:
        reason = f"version 为 {manifest.version}，与 instance.yaml 引用的版本 {version} 不同"
        errors.add_at(name, PACK_FILE, "version", reason)
        mismatched = True
    if mismatched:
        return None
    return PackSource(name=name, version=version, directory=directory, manifest=manifest)


def _read[M: BaseModel](
    model: type[M], path: Path, pack: str | None, file: str, errors: StepErrors
) -> M | None:
    """Read ``path`` and validate the document it holds against ``model``.

    A file that cannot be read or parsed is one error at its line
    (:attr:`~erp_mock.loader.yaml_io.YamlLoadError.config_path`); a document that does not
    fit the model is one error per pydantic error, each at its config path. Returns None
    in both cases, having recorded them, so the caller stops reading that document. An
    empty document is None and does not fit any model, which the model reports.
    """
    try:
        document = load_yaml(path)
    except YamlLoadError as error:
        errors.add_at(pack, file, error.config_path, error.reason)
        return None
    try:
        return model.model_validate(document)
    except ValidationError as error:
        errors.add_model_errors(pack, file, error)
        return None

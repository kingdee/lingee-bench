"""The order of the validation steps and the frame that collects and raises their errors.

:class:`Loader` validates one instance config together with the business packs its
instance.yaml references (requirement 2.1). ``load`` runs the 8 steps of
business-pack.md section 6 in order and then the cross-check of descriptions against
Business_Data (:data:`~erp_mock.loader.errors.CROSS`, requirement 3.5), the sequence of
the table in design.md 2.3:

=======  =============================================================================
step     checks
=======  =============================================================================
1        instance.yaml and the pack references, with each pack.yaml manifest
2        requires_meta_model
3        depends_on: ranges, referenced packs, versions, cycles
4        every configuration file against its model, and the instance profiles/datasets
5        references, namespaces, prefixes, rule IDs, primitive use, expressions
6        tool generation: names, prefixes, rename and exclude, duplicates, profiles
7        description variants: completeness, length, guiding phrases, described rules
8        the Snapshot of each dataset: INV-1, INV-2, INV-3, INV-6, current user, org
cross    descriptions must not contain names, codes or numbers of Business_Data
=======  =============================================================================

Each step reports every error it finds instead of stopping at the first one: the step
collects them in its own :class:`~erp_mock.loader.errors.StepErrors` and, when that
collector is not empty, raises :class:`~erp_mock.loader.errors.ConfigErrors`, so the
steps after it do not run (requirement 2.2). They could not run anyway, since every step
builds on the results of the ones before it.

This module owns the order and that frame only. :meth:`Loader.check` is where the checks
of a step are plugged in, one step module at a time (``loader.step1`` and the others);
a step reads what the earlier steps left on the loader, records its errors in the
collector it is given and leaves its own result for the steps that follow. When every step
passed, :meth:`Loader.assemble` puts those results together into the read-only
:class:`~erp_mock.loader.instance.LoadedInstance` of design.md 2.4, which is what
:meth:`Loader.load` returns.

Every step is implemented (``loader.step1`` to ``loader.step8``), so ``load`` validates
instance.yaml, the pack manifests, the dependencies between the packs and every
configuration file of those packs together with the profiles and datasets of the instance,
resolves the references between the packs into the domain model, checks everything step 5
reads from those documents - the namespaces, the prefixes, the rule numbers, the primitive
use, the context entities, the references of the instance configuration, the field and
linkage declarations and the rule expressions - generates the tools of every pack and
checks their names, checks the descriptions of those tools, takes up the Snapshot of every
initial dataset and checks it, and ends by comparing those descriptions against the records
of the Snapshots.
"""

from datetime import date
from pathlib import Path
from typing import Final

from erp_mock.config.instance import DatasetDef, InstanceDef, ProfileDef
from erp_mock.config.model import DomainModel
from erp_mock.loader import step1, step2, step3, step4, step5, step6, step7, step8
from erp_mock.loader.errors import CROSS, Step, StepErrors
from erp_mock.loader.instance import LoadedInstance
from erp_mock.loader.step1 import PackSource
from erp_mock.loader.step4 import RawDoc, RawPack
from erp_mock.loader.step5_expr import ParsedDomain
from erp_mock.loader.step6 import GeneratedTools
from erp_mock.loader.step8 import Step8
from erp_mock.tools.descriptions import DescriptionIndex

__all__ = ["STEPS", "Loader"]

# The validation steps in the order they run.
STEPS: Final[tuple[Step, ...]] = (1, 2, 3, 4, 5, 6, 7, 8, CROSS)


class Loader:
    """Loads one instance config and the business packs it references (requirement 2.1).

    - ``instance_dir``: the directory of the instance config (the CLI's ``--instance``).
    - ``packs_dir``: the directory that holds the business packs (``--packs-dir``);
      a directory in it that instance.yaml does not reference is not read.
    - ``anchor_date``: the anchor date step 8 takes up the Snapshots under, None when it is
      to be taken from the clock configuration (:func:`~erp_mock.loader.step8.anchor_of`,
      requirement 2.14).
    - ``data_dir``: the data directory (``--data-dir``) whose ``snapshots/`` holds the
      Snapshot cache; step 8 needs it and the steps before it do not, so it may be left out
      while only those are run.
    - ``pack_order``: the pack names in the order of instance.yaml, which decides the
      order of the reported errors; step 1 fills it in as soon as it has read
      instance.yaml.

    What a step leaves for the steps after it is an attribute too. Step 1 leaves:

    - ``instance``: the validated instance.yaml, None until step 1 has read it.
    - ``packs``: one :class:`~erp_mock.loader.step1.PackSource` per referenced pack, in
      the order of instance.yaml, each with the pack directory and its manifest.

    Steps 2 and 3 read those packs and leave nothing of their own: they settle that each
    manifest is written against this meta model version and that the dependencies between
    the packs resolve without a cycle. Step 4 reads the configuration files and leaves:

    - ``raw_packs``: one :class:`~erp_mock.loader.step4.RawPack` per pack, in the order of
      instance.yaml, holding every validated document of that pack with its file.
    - ``raw_profiles`` and ``raw_datasets``: the validated profile and dataset definitions
      of the names instance.yaml lists, in its order, each with its file.

    Step 5 resolves those documents into the domain model, checks their rule expressions
    and leaves:

    - ``parsed``: the :class:`~erp_mock.loader.step5_expr.ParsedDomain` of the instance,
      the domain model together with the syntax tree of every rule expression of it, None
      until step 5 has built it.
    - ``domain``: the :class:`~erp_mock.config.model.DomainModel` of ``parsed``, the model
      the steps after step 5 read; it is the configuration of a valid instance only when
      step 5 reported nothing.

    Step 6 generates the tools of that model and checks their names, and leaves:

    - ``tools``: the :class:`~erp_mock.loader.step6.GeneratedTools` of the instance, the
      built-in tools and the tools of each pack in output order, None until step 6 has
      generated them.

    Step 7 checks the descriptions of those tools and leaves:

    - ``descriptions``: the :class:`~erp_mock.tools.descriptions.DescriptionIndex` of the
      instance, every description document by pack and variant, None until step 7 has read
      them; the profile assembly puts that text into the tools of a profile
      (requirement 3.7).

    Step 8 takes up the Snapshot of every initial dataset and checks it, and leaves:

    - ``step8``: the :class:`~erp_mock.loader.step8.Step8` of the instance, one
      :class:`~erp_mock.loader.instance.DatasetModel` per dataset together with the
      Business_Data of their Snapshots, None until step 8 has run; the cross-check compares
      the descriptions against that data (requirement 3.5).
    """

    def __init__(
        self,
        instance_dir: Path,
        packs_dir: Path,
        anchor_date: date | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.instance_dir = instance_dir
        self.packs_dir = packs_dir
        self.anchor_date = anchor_date
        self.data_dir = data_dir
        self.pack_order: tuple[str, ...] = ()
        self.instance: InstanceDef | None = None
        self.packs: tuple[PackSource, ...] = ()
        self.raw_packs: tuple[RawPack, ...] = ()
        self.raw_profiles: tuple[RawDoc[ProfileDef], ...] = ()
        self.raw_datasets: tuple[RawDoc[DatasetDef], ...] = ()
        self.parsed: ParsedDomain | None = None
        self.domain: DomainModel | None = None
        self.tools: GeneratedTools | None = None
        self.descriptions: DescriptionIndex | None = None
        self.step8: Step8 | None = None

    def load(self) -> LoadedInstance:
        """Run every step in order and return the read-only configuration object.

        Raises :class:`~erp_mock.loader.errors.ConfigErrors` at the first step that finds
        errors, so the object is assembled only for an instance that passed every step
        (requirement 2.11).
        """
        for step in STEPS:
            self.run_step(step)
        return self.assemble()

    def assemble(self) -> LoadedInstance:
        """The read-only configuration object of what the steps left behind (design.md 2.4).

        Every step that produces a result must have run; a loader whose steps found errors
        never reaches this, since each of them raises. The object is assembled without the
        datasets while the steps up to 7 are being run on their own, which is also how
        step 8 gets the configuration a build reads (:meth:`anchor` decides the anchor date
        either way).
        """
        if (
            self.instance is None
            or self.domain is None
            or self.tools is None
            or self.descriptions is None
        ):
            raise AssertionError("加载步骤没有全部运行，无法组装只读配置对象")
        return LoadedInstance.build(
            instance=self.instance,
            packs=self.raw_packs,
            domain=self.domain,
            profiles=self.raw_profiles,
            datasets=self.raw_datasets,
            tools=self.tools,
            descriptions=self.descriptions,
            step8_anchor=self.anchor(),
            dataset_models=() if self.step8 is None else self.step8.datasets,
        )

    def anchor(self) -> date:
        """The anchor date step 8 takes up the Snapshots under (requirement 2.14).

        The ``anchor_date`` of this loader, or the ``anchor_date`` of a fixed clock
        (:func:`~erp_mock.loader.step8.anchor_of`); raises AssertionError before step 1 has
        read instance.yaml, and for a clock in mode today without an ``anchor_date``, which
        the caller resolves through the gateway's SystemClock.
        """
        if self.instance is None:
            raise AssertionError("第 1 步没有读入 instance.yaml，无法确定第 8 步锚点日期")
        return step8.anchor_of(self.instance, self.anchor_date)

    def run_step(self, step: Step) -> None:
        """Run the checks of ``step`` and raise ConfigErrors when it found any error.

        The errors are sorted with the pack order known at that point, which step 1 sets.
        """
        errors = StepErrors(step)
        self.check(step, errors)
        errors.raise_if_any(self.pack_order)

    def check(self, step: Step, errors: StepErrors) -> None:
        """Run the checks of ``step``, recording what they find in ``errors``.

        The extension point of the steps (see the module docstring): the checks of each
        step are plugged in here. Recording nothing means the step passed.
        """
        if step == 1:
            result = step1.check(self.instance_dir, self.packs_dir, errors)
            if result is not None:
                self.instance = result.instance
                self.packs = result.packs
                # The pack order of instance.yaml, which sorts the errors of this step
                # and of every step after it, the packs in error included.
                self.pack_order = tuple(result.instance.packs)
        elif step == 2:
            step2.check(self.packs, errors)
        elif step == 3:
            step3.check(self.packs, errors)
        elif step == 4 and self.instance is not None:
            # Step 1 ends the loading when instance.yaml is in error, so the instance is
            # there whenever this step runs as part of load().
            raw = step4.check(self.instance_dir, self.instance, self.packs, errors)
            self.raw_packs = raw.packs
            self.raw_profiles = raw.profiles
            self.raw_datasets = raw.datasets
        elif step == 5 and self.instance is not None:
            # The instance is there for the same reason it is at step 4. The model and the
            # parsed expressions are kept whatever the step found, so a test can read what
            # resolved; the steps after this one run only when the step reported nothing.
            self.parsed = step5.check(
                self.raw_packs, self.instance, self.raw_profiles, self.raw_datasets, errors
            )
            self.domain = self.parsed.domain
        elif step == 6 and self.domain is not None:
            # Step 5 always leaves the model behind, so it is there whenever this step
            # runs as part of load(). The tools are kept whatever the step found, so a
            # test can read what was generated.
            self.tools = step6.check(self.raw_packs, self.domain, self.raw_profiles, errors)
        elif step == 7 and self.domain is not None and self.tools is not None:
            # Step 6 always leaves the tools behind, as step 5 does the model, so both are
            # there whenever this step runs as part of load(). The descriptions are kept
            # whatever the step found, so a test can read what was written.
            self.descriptions = step7.check(self.raw_packs, self.domain, self.tools, errors)
        elif step == 8 and self.parsed is not None and self.descriptions is not None:
            # Step 7 always leaves the descriptions behind, so everything the read-only
            # configuration object is assembled from is there whenever this step runs as
            # part of load(); that object is what a build of a dataset reads. The datasets
            # and the Business_Data are kept whatever the step found, so a test can read
            # what was taken up.
            data_dir = self._require_data_dir()
            self.step8 = step8.check(
                self.assemble(),
                self.raw_datasets,
                self.instance_dir,
                data_dir,
                errors,
                engine=step8.engine_of(self.parsed),
            )
        elif step == CROSS and self.tools is not None and self.descriptions is not None:
            # Step 8 always leaves its result behind, so the Business_Data of the Snapshots
            # is there whenever the cross-check runs as part of load().
            data = Step8().data if self.step8 is None else self.step8.data
            step8.cross_check(self.raw_packs, self.tools, self.descriptions, data, errors)

    def _require_data_dir(self) -> Path:
        """The data directory step 8 caches the Snapshots below (requirement 20.11)."""
        if self.data_dir is None:
            raise AssertionError("第 8 步需要数据目录（CLI 的 --data-dir），用来缓存 Snapshot")
        return self.data_dir

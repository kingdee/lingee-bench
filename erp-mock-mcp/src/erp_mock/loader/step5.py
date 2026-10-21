"""Loading step 5: the configuration of the packs as a whole (design.md 2.3).

Steps 1 to 4 settled each document on its own: it parses, it fits the model of its kind and
the pack that holds it is referenced, written against this meta model version and depends
only on packs of this instance. The fifth step is the first one that reads the packs
together, which is where everything that needs more than one document belongs:

================  ========================================================  ==============
part              checks                                                    requirements
================  ========================================================  ==============
step5_model       the domain model; references between packs                 1.3 to 1.5, 2.9
step5_namespace   namespaces, prefixes, rule numbers, primitive use,         1.14, 2.6 to
                  context entities, profile and dataset references           2.8, 2.10
step5_fields      same-name declarations of standard fields, quantity        1.6, 6.12,
                  semantics, inventory linkages, simple state machines,      7.11, 9.3,
                  the order of the defaults                                  9.11
step5_expr        the rule expressions of the rules, writebacks and          10.2
                  declarative queries
================  ========================================================  ==============

This module is the entry point and the order: :func:`check` builds the domain model first,
since every other part of the step reads the resolved model instead of the documents, and
then runs the parts that check it. Each part records what it finds in the one
:class:`~erp_mock.loader.errors.StepErrors` of the step, so one run reports the errors of
all of them (requirement 2.2); a reference that did not resolve leaves None in the model,
and a part that would read it skips that check and goes on. A part that settles something
the engine later reads leaves it in the model and hands the model on: ``step5_fields``
adds the order the ``default_from`` defaults of each level are applied in, and
``step5_expr`` the syntax tree of every rule expression
(:class:`~erp_mock.loader.step5_expr.ParsedDomain`, which pairs the two because the
configuration layer may not hold a tree of the domain layer).

The result is returned whatever was found, so the loader keeps it for the steps after this
one (``Loader.domain`` and ``Loader.parsed``); it is the configuration of a valid instance
only when the step reported nothing.
"""

from collections.abc import Sequence

from erp_mock.config.instance import DatasetDef, InstanceDef, ProfileDef
from erp_mock.loader import step5_expr, step5_fields, step5_model, step5_namespace
from erp_mock.loader.errors import StepErrors
from erp_mock.loader.step4 import RawDoc, RawPack
from erp_mock.loader.step5_expr import ParsedDomain

__all__ = ["check"]


def check(
    packs: Sequence[RawPack],
    instance: InstanceDef,
    profiles: Sequence[RawDoc[ProfileDef]],
    datasets: Sequence[RawDoc[DatasetDef]],
    errors: StepErrors,
) -> ParsedDomain:
    """Run step 5 on ``packs`` and the instance; return the model with its syntax trees.

    ``packs`` are the packs instance.yaml references, in its order, with the documents
    step 4 validated; ``instance`` is that instance.yaml and ``profiles`` and ``datasets``
    the definitions step 4 read for the names it lists. Everything the parts find is
    recorded in ``errors``, so the step passed only when ``errors`` stayed empty.
    """
    domain = step5_model.build(packs, errors)
    step5_namespace.check(packs, instance, profiles, datasets, errors)
    domain = step5_fields.check(packs, domain, errors)
    return step5_expr.check(packs, domain, errors)

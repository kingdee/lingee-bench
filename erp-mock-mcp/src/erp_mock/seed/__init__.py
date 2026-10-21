"""Dataset snapshots: seed builder, value generators, blacklist and snapshot cache.

An initial dataset is built, never written by hand: the Seed_Builder replays the dataset
definition through the engine on an empty database and writes the result out as a
Snapshot with its metadata and alias map, which a sandbox then copies (requirement 20).
The modules of this layer each take one part of that:

- :mod:`~erp_mock.seed.generators`: the seven built-in value generators, the real company
  name blacklist of ``blacklist.txt`` and the discarding of blacklisted and duplicate
  values (requirements 20.3, 20.7).
- :mod:`~erp_mock.seed.resolve`: the static pass over a dataset definition - kinds,
  fields, generators and their parameters, aliases, refs, date expressions and statuses
  (requirements 20.1, 20.6, 20.16).
- :mod:`~erp_mock.seed.builder`: the build itself - shared data, the dataset's own
  records and the bulk records through the engine, the invariant checks, and the three
  output files (requirements 20.2, 20.5, 20.9, 20.10, 20.17).
- :mod:`~erp_mock.seed.cache`: the cache under ``--data-dir``, keyed by dataset version
  and anchor date, with its invalidation and its atomic replacement
  (requirements 20.11, 20.12).

Randomness comes only from the dataset's random_seed via random.Random instances.
"""

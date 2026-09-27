"""Market Query Language (MQL).

MQL is a controlled semantic layer over the published Atlas dataset, not a general
SQL frontend (see ``docs/MQL.md``). A query names metrics the
:mod:`~vizanix_atlas.core.metrics_registry` declares; the compiler is the only thing
that ever sees a physical column name or a table path, and the physical layer it
targets is fixed to the local Parquet files a client already downloaded. There is no
remote query server: every query in this package runs entirely against files already
on disk (see ``docs/GITHUB_ARCHITECTURE.md`` on "no dynamic backend").

The pipeline is: text -> tokens -> an AST -> a semantically validated AST -> a
parameterised DuckDB SQL string -> a result table. Each stage is its own module so
that a query can be inspected (``atlas query --explain``) at any point before it runs.
"""

from __future__ import annotations

# Market Query Language (MQL)

MQL asks questions of the whole market without writing exchange-specific code:

```sql
SELECT asset, reference_price, venue_count, price_dispersion_bps, reported_volume_24h_usd
FROM market
WHERE venue_count >= 8 AND reported_volume_24h_usd > 10000000
ORDER BY price_dispersion_bps DESC
LIMIT 20;
```

It runs **locally**, against Parquet files you have downloaded. There is no query server. `atlas query` and `Atlas.query()` download the shards once (checksum-verified, cached) and run everything on your machine.

## It is not SQL

MQL is a controlled semantic layer that *compiles to* SQL. The compiler emits one parameterised DuckDB statement of a fixed shape, over a `market` view built from an explicit list of files Atlas itself chose. There are no function calls, joins, sub-queries, other tables, file or network access, and no user text is ever interpolated into SQL: identifiers are resolved against the metric registry and values are bound parameters.

## Grammar

```
query        := SELECT select_list FROM ident where? order_by? limit? ';'?
select_list  := '*' | select_item (',' select_item)*
select_item  := ident ('AS' ident)?
where        := 'WHERE' bool_expr
order_by     := 'ORDER' 'BY' order_item (',' order_item)*
order_item   := ident ('ASC' | 'DESC')?
limit        := 'LIMIT' number

bool_expr    := or_expr
or_expr      := and_expr ('OR' and_expr)*
and_expr     := not_expr ('AND' not_expr)*
not_expr     := 'NOT' not_expr | predicate
predicate    := '(' bool_expr ')'
              | operand 'IS' 'NOT'? 'NULL'
              | operand 'NOT'? 'IN' '(' literal (',' literal)* ')'
              | operand 'NOT'? 'BETWEEN' operand 'AND' operand
              | operand comparison_op operand
operand      := ident | literal
literal      := '-'? number | string | 'TRUE' | 'FALSE' | 'NULL'
```

Comparison operators: `=  !=  <  <=  >  >=`. Keywords are case-insensitive. Strings use single quotes. The only source is `market` (one row per asset).

## Pipeline

`text → tokens → AST → validated AST → SQL + parameters → DuckDB → Polars`. A hand-written lexer and recursive-descent parser (`mql/lexer.py`, `mql/parser.py`) produce a typed AST; semantic validation resolves every identifier against `mql/columns.py` and the [metric registry](METRICS.md); the compiler (`mql/compiler.py`) emits SQL; the engine (`mql/engine.py`) executes it.

## Errors

Errors are typed and specific, with positions and suggestions:

```
MqlSemanticError: unknown metric 'reference_prise'. Did you mean: reference_price, reference_price_method, reference_price_venue_count? (position=7)
MqlSyntaxError: expected RPAREN but found <end of query> (position=47)
```

## Missing data

A null stays null: `reference_price IS NULL` finds assets with no price; comparisons with null are false, as in SQL. Nothing is coerced to zero.

## Which metrics can I query?

Those marked *Published = yes* in the [metric dictionary](METRICS.md). Metrics that need history (`CHANGE`, `PERCENTILE`, windowed volatility) are declared but not yet backed by data, and MQL does not offer functions it cannot honour.

## Inspecting a query

`vizanix_atlas.mql.engine.explain(text)` returns the parsed AST, the validated query and the compiled SQL with parameters, so you can see exactly what will run.

## Browser

The website's Scanner page applies the same kinds of filters client-side to the exported JSON and shows the equivalent MQL. The Python/CLI implementation is authoritative; there is no in-browser MQL engine.

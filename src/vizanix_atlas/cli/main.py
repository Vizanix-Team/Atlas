"""The ``atlas`` command-line interface.

Every command that reads a dataset accepts ``--source``, defaulting to the current
published generation (downloaded and cached transparently, see ``Atlas.latest()``); a
local path (a ``FilesystemPublisher`` root or a bare generation directory) or a
specific ``atlas-data-<id>`` generation ID both work too. ``--json`` switches any
command's output to machine-readable JSON, for scripting (``docs/OPERATIONS.md``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vizanix_atlas.core.atlas_time import day_label, to_epoch_ms, utc_now
from vizanix_atlas.core.config import (
    load_collection_config,
    load_exchange_registry,
    load_retention_config,
)
from vizanix_atlas.core.errors import AtlasError
from vizanix_atlas.core.versions import (
    DATASET_FORMAT_VERSION,
    METHODOLOGY_VERSION,
    SCHEMA_VERSION,
    SOFTWARE_VERSION,
)
from vizanix_atlas.discovery.collector import CollectionRequest, collect_venue
from vizanix_atlas.discovery.pipeline import build_generation
from vizanix_atlas.models.manifest import DailyCompactionReport, GenerationManifest
from vizanix_atlas.models.observations import CollectionResult
from vizanix_atlas.publishing.daily import compact_day
from vizanix_atlas.publishing.publish import publish_generation
from vizanix_atlas.publishing.publisher import (
    FilesystemPublisher,
    GitHubReleasePublisher,
    Publisher,
)
from vizanix_atlas.publishing.retention import find_candidates
from vizanix_atlas.publishing.validator import (
    validate_checksums,
    validate_row_counts,
    validate_schema,
)
from vizanix_atlas.sdk.client import Atlas
from vizanix_atlas.sdk.remote import ReleaseDownloader
from vizanix_atlas.storage.dataset_builder import build_dataset_files
from vizanix_atlas.storage.housekeeping import ExecutionReport, execute_cleanup, plan_cleanup

app = typer.Typer(
    name="atlas",
    help="Vizanix Atlas: the semantic layer for global crypto markets.",
    no_args_is_help=True,
)
console = Console()
error_console = Console(stderr=True)


def _open(source: str | None) -> Atlas:
    """Resolve ``--source`` into an open :class:`Atlas` handle.

    ``None`` means the current published generation (``Atlas.latest()``); anything
    else is treated as a local path first, then as a generation ID.
    """
    if source is None:
        return Atlas.latest()
    path = Path(source)
    if path.exists():
        return Atlas.from_local(path)
    return Atlas.from_generation(source)


def _fail(error: AtlasError) -> typer.Exit:
    error_console.print(f"[bold red]error:[/bold red] {error}")
    return typer.Exit(code=1)


SourceOption = Annotated[
    str | None,
    typer.Option(
        "--source", help="A local dataset path or generation ID. Defaults to the latest release."
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
]


@app.command()
def status(source: SourceOption = None, as_json: JsonOption = False) -> None:
    """Show the currently open generation's identity and collection outcome."""
    try:
        atlas = _open(source)
    except AtlasError as error:
        raise _fail(error) from error
    manifest = atlas.manifest
    payload = {
        "generation_id": manifest.generation_id,
        "slot_label": manifest.slot_label,
        "published_at": manifest.published_at,
        "schema_version": manifest.schema_version,
        "methodology_version": manifest.methodology_version,
        "software_version": manifest.software_version,
        "asset_count": manifest.asset_count,
        "instrument_count": manifest.instrument_count,
        "venue_count": manifest.venue_count,
        "venues_successful": manifest.venues.successful,
        "venues_degraded": manifest.venues.degraded,
        "venues_failed": manifest.venues.failed,
        "venues_disabled": manifest.venues.disabled,
    }
    if as_json:
        console.print_json(json.dumps(payload))
        return
    table = Table(title=f"Atlas generation {manifest.generation_id}", show_header=False)
    for key, value in payload.items():
        table.add_row(key, str(value))
    console.print(table)


@app.command()
def asset(
    symbol: Annotated[str, typer.Argument(help="A ticker symbol or a full canonical asset ID.")],
    source: SourceOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show one asset's published Universal Market State."""
    try:
        atlas = _open(source)
        view = atlas.asset(symbol)
    except AtlasError as error:
        raise _fail(error) from error
    if as_json:
        console.print_json(json.dumps(view.raw(), default=str))
        return

    row = view.raw()
    console.print(f"[bold]{row.get('symbol')}[/bold]  {row.get('name') or ''}")
    console.print(f"  asset_id            {row.get('asset_id')}")
    console.print(f"  resolution_state    {row.get('resolution_state')}")
    console.rule("Reference price")
    console.print(f"  value               {row.get('reference_price__value')}")
    console.print(f"  method              {row.get('reference_price__method')}")
    console.print(f"  venue_count         {row.get('reference_price__venue_count')}")
    console.rule("Volume")
    console.print(f"  reported_24h_usd    {row.get('reported_volume_24h_usd')}")
    console.rule("Derivatives")
    console.print(f"  funding_8h_median   {row.get('funding__rate_8h_median')}")
    console.print(f"  open_interest_usd   {row.get('open_interest__total_usd')}")
    console.print(f"  perp_basis_bps      {row.get('basis__perp_basis_bps_median')}")
    console.rule("Liquidity")
    console.print(f"  best_spread_bps     {row.get('liquidity__best_spread_bps')}")
    console.print(f"  venue_count         {row.get('liquidity__venue_count')}")
    console.rule("Quality")
    console.print(f"  coverage_ratio      {row.get('quality__coverage__coverage_ratio')}")
    console.print(f"  venue_count         {row.get('venue_count')}")
    console.print(f"  is_partial          {row.get('is_partial')}")


@app.command()
def search(text: str, source: SourceOption = None, as_json: JsonOption = False) -> None:
    """Find canonical assets by symbol or name (discovery, not identity resolution)."""
    try:
        atlas = _open(source)
        matches = atlas.search(text)
    except AtlasError as error:
        raise _fail(error) from error
    if as_json:
        console.print_json(json.dumps(matches))
        return
    if not matches:
        console.print("no matching assets")
        return
    table = Table()
    for column in ("symbol", "name", "resolution_state", "asset_id"):
        table.add_column(column)
    for row in matches:
        table.add_row(
            *(str(row.get(c, "")) for c in ("symbol", "name", "resolution_state", "asset_id"))
        )
    console.print(table)


@app.command()
def market(
    source: SourceOption = None,
    top: Annotated[
        int, typer.Option(help="How many assets to show, ranked by reported volume.")
    ] = 20,
    as_json: JsonOption = False,
) -> None:
    """Show a market-wide summary: asset counts and top assets by reported volume."""
    try:
        atlas = _open(source)
        view = atlas.market()
    except AtlasError as error:
        raise _fail(error) from error
    top_frame = view.top_by_volume(top)
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "asset_count": view.asset_count,
                    "qualified_asset_count": view.qualified_asset_count(),
                    "top_by_volume": top_frame.select(
                        "symbol", "reported_volume_24h_usd", "venue_count"
                    ).to_dicts(),
                }
            )
        )
        return
    console.print(
        f"assets: {view.asset_count}   multi-venue: {view.qualified_asset_count(min_venue_count=2)}"
    )
    table = Table(title=f"Top {top} by reported 24h volume")
    table.add_column("asset")
    table.add_column("reported_volume_24h_usd", justify="right")
    table.add_column("venue_count", justify="right")
    for row in top_frame.select("symbol", "reported_volume_24h_usd", "venue_count").to_dicts():
        table.add_row(
            str(row["symbol"]), str(row["reported_volume_24h_usd"]), str(row["venue_count"])
        )
    console.print(table)


@app.command()
def query(
    text: Annotated[
        str, typer.Argument(help="An MQL query, e.g. SELECT asset FROM market LIMIT 10.")
    ],
    source: SourceOption = None,
    as_json: JsonOption = False,
) -> None:
    """Run a Market Query Language query against a generation's downloaded shards."""
    try:
        atlas = _open(source)
        result = atlas.query(text)
    except AtlasError as error:
        raise _fail(error) from error
    if as_json:
        console.print_json(json.dumps(result.to_dicts(), default=str))
        return
    table = Table()
    for column in result.columns:
        table.add_column(column)
    for row in result.iter_rows():
        table.add_row(*(str(v) for v in row))
    console.print(table)


@app.command()
def exchanges(as_json: JsonOption = False) -> None:
    """List every declared venue and whether it is enabled."""
    registry = load_exchange_registry()
    rows = [
        {
            "slug": v.slug,
            "display_name": v.display_name,
            "enabled": v.enabled,
            "priority": v.priority,
            "disabled_reason": v.disabled_reason,
        }
        for v in registry.venues
    ]
    if as_json:
        console.print_json(json.dumps(rows))
        return
    table = Table()
    for column in ("slug", "display_name", "priority", "enabled", "disabled_reason"):
        table.add_column(column)
    for row in sorted(rows, key=lambda r: (not r["enabled"], r["slug"])):
        table.add_row(
            str(row["slug"]),
            str(row["display_name"]),
            str(row["priority"]),
            "yes" if row["enabled"] else "no",
            str(row["disabled_reason"] or ""),
        )
    console.print(table)


@app.command()
def methodology() -> None:
    """Print Atlas's version identities and a pointer to the full methodology doc."""
    console.print(f"software_version:     {SOFTWARE_VERSION}")
    console.print(f"schema_version:       {SCHEMA_VERSION}")
    console.print(f"methodology_version:  {METHODOLOGY_VERSION}")
    console.print(f"dataset_format_version: {DATASET_FORMAT_VERSION}")
    console.print("\nSee docs/METHODOLOGY.md for the full derivation of every published metric.")


@app.command()
def verify(
    source: Annotated[str, typer.Argument(help="A local generation directory or dataset root.")],
) -> None:
    """Verify a local dataset's manifest, checksums and row counts."""
    path = Path(source)
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        pointer_path = path / "data-latest" / "latest.json"
        if not pointer_path.is_file():
            error_console.print(f"[bold red]error:[/bold red] no manifest.json found under {path}")
            raise typer.Exit(code=1)
        from vizanix_atlas.models.manifest import LatestPointer

        pointer = LatestPointer.model_validate_json(pointer_path.read_text(encoding="utf-8"))
        path = path / f"atlas-data-{pointer.generation_id}"
        manifest_path = path / "manifest.json"

    manifest = GenerationManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    checks = {
        "Manifest": validate_schema(manifest),
        "Checksums": validate_checksums(manifest, path),
        "Row counts": validate_row_counts(manifest),
    }
    all_passed = True
    for name, report in checks.items():
        status_text = "[green]OK[/green]" if report.passed else "[red]FAILED[/red]"
        console.print(f"{name:<12} {status_text}")
        if not report.passed:
            all_passed = False
            for issue in report.issues:
                console.print(f"  - {issue.check}: {issue.detail}")
    console.print(f"Generation ID    {manifest.generation_id}")
    console.print(f"File count       {len(manifest.files)}")
    if not all_passed:
        raise typer.Exit(code=1)


@app.command()
def collect(
    exchange: Annotated[str, typer.Option(help="The venue slug to collect from, e.g. okx.")],
    output: Annotated[
        Path, typer.Option(help="Directory to write the collection result JSON into.")
    ],
) -> None:
    """Collect one venue's tier-A observations and write them as a CollectionResult."""
    registry = load_exchange_registry()
    config = load_collection_config()
    try:
        venue = registry.get(exchange)
    except AtlasError as error:
        raise _fail(error) from error

    run_id = f"local-{to_epoch_ms(utc_now())}"
    request = CollectionRequest(venue=venue, run_id=run_id)
    result = asyncio.run(collect_venue(request, config))

    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{venue.slug}.json"
    destination.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    console.print(
        f"{venue.slug}: {result.health.status}  "
        f"instruments={result.health.instrument_count}  tickers={result.health.ticker_count}  "
        f"-> {destination}"
    )


@app.command()
def aggregate(
    input_dir: Annotated[
        Path, typer.Argument(help="Directory of per-venue CollectionResult JSON files.")
    ],
) -> None:
    """Build a generation from collected results and print a summary, without writing anything."""
    results = _read_collection_results(input_dir)
    if not results:
        error_console.print(
            f"[bold red]error:[/bold red] no CollectionResult files found under {input_dir}"
        )
        raise typer.Exit(code=1)
    config = load_collection_config()
    generation = build_generation(results, config=config)
    console.print(f"generation_id     {generation.generation_id}")
    console.print(f"asset_count       {generation.asset_count}")
    console.print(f"instrument_count  {generation.instrument_count}")
    console.print(f"venue_count       {generation.venue_count}")
    console.print(f"resolution        {generation.resolution_summary}")
    console.print(f"quarantine        {generation.quarantine_summary}")


def _build_and_publish(input_dir: Path, staging_root: Path, publisher: Publisher) -> None:
    """Build a generation from collected results and offer it to ``publisher``.

    Exits non-zero when the validity gate refuses the generation, so a workflow can
    surface "the previous generation remains canonical" instead of reporting success.
    """
    results = _read_collection_results(input_dir)
    if not results:
        error_console.print(
            f"[bold red]error:[/bold red] no CollectionResult files found under {input_dir}"
        )
        raise typer.Exit(code=1)
    config = load_collection_config()
    generation = build_generation(results, config=config)

    staging_dir = staging_root / generation.generation_id
    built = build_dataset_files(generation, staging_dir, config=config)
    outcome = asyncio.run(
        publish_generation(
            generation,
            dataset_dir=staging_dir,
            written_files=list(built.written_files),
            publisher=publisher,
            config=config,
            release_tag=f"atlas-data-{generation.generation_id}",
            shard_indices=built.shard_indices,
        )
    )
    if not outcome.published:
        error_console.print(f"[bold red]not published:[/bold red] {outcome.reason}")
        raise typer.Exit(code=1)
    console.print(f"published generation {generation.generation_id}")
    console.print(f"asset_count       {generation.asset_count}")
    console.print(f"instrument_count  {generation.instrument_count}")


@app.command(name="build-dataset")
def build_dataset(
    input_dir: Annotated[
        Path, typer.Option("--input", help="Directory of per-venue CollectionResult JSON files.")
    ],
    output: Annotated[
        Path, typer.Option("--output", help="Directory to publish the dataset into.")
    ],
) -> None:
    """Build and locally publish a complete dataset generation from collected results."""
    _build_and_publish(input_dir, output / ".staging", FilesystemPublisher(output))


@app.command(name="publish-github")
def publish_github(
    input_dir: Annotated[
        Path, typer.Option("--input", help="Directory of per-venue CollectionResult JSON files.")
    ],
    repository: Annotated[
        str,
        typer.Option(
            envvar="GITHUB_REPOSITORY", help="The owner/name of the repository to publish into."
        ),
    ],
    token: Annotated[
        str,
        typer.Option(envvar="GITHUB_TOKEN", help="A token allowed to write releases."),
    ],
    staging: Annotated[Path, typer.Option(help="Local scratch directory for built files.")] = Path(
        ".atlas-staging"
    ),
) -> None:
    """Build a generation and publish it to GitHub Releases (used by the collect workflow)."""
    owner, _, repo = repository.partition("/")
    publisher = GitHubReleasePublisher(owner=owner, repo=repo, token=token)
    try:
        _build_and_publish(input_dir, staging, publisher)
    finally:
        asyncio.run(publisher.aclose())


@app.command(name="compact-day")
def compact_day_command(
    day: Annotated[
        str | None, typer.Option(help="UTC day to compact, YYYY-MM-DD. Defaults to yesterday.")
    ] = None,
    repository: Annotated[str, typer.Option(envvar="GITHUB_REPOSITORY")] = "",
    token: Annotated[str, typer.Option(envvar="GITHUB_TOKEN")] = "",
) -> None:
    """Compact one UTC day's generations into a single verified daily release."""
    target = day or day_label(utc_now() - timedelta(days=1))
    owner, _, repo = repository.partition("/")
    publisher = GitHubReleasePublisher(owner=owner, repo=repo, token=token)
    source = ReleaseDownloader(owner=owner, repo=repo)

    async def run() -> DailyCompactionReport:
        try:
            return await compact_day(
                target, publisher=publisher, source=source, config=load_collection_config()
            )
        finally:
            await publisher.aclose()

    report = asyncio.run(run())
    console.print(
        f"{target}: compacted={report.compacted} "
        f"successful={len(report.successful_windows)}/{len(report.expected_windows)} "
        f"missing={len(report.missing_windows)} duplicate={len(report.duplicate_windows)}"
    )
    if not report.compacted:
        raise typer.Exit(code=1)


@app.command()
def housekeeping(
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually delete. Without this flag nothing changes.")
    ] = False,
    repository: Annotated[str, typer.Option(envvar="GITHUB_REPOSITORY")] = "",
    token: Annotated[str, typer.Option(envvar="GITHUB_TOKEN")] = "",
) -> None:
    """Plan (and with --apply, carry out) retention cleanup of compacted intra-day files."""
    retention = load_retention_config()
    owner, _, repo = repository.partition("/")
    publisher = GitHubReleasePublisher(owner=owner, repo=repo, token=token)
    source = ReleaseDownloader(owner=owner, repo=repo)

    async def run() -> ExecutionReport:
        try:
            candidates = await find_candidates(publisher, source, retention)
            plan = plan_cleanup(candidates, retention.housekeeping)
            console.print(plan.summary())
            return await execute_cleanup(
                plan,
                publisher,
                dry_run=not apply,
                log_every_deletion=retention.housekeeping.log_every_deletion,
            )
        finally:
            await publisher.aclose()

    outcome = asyncio.run(run())
    mode = "dry run" if outcome.dry_run else "deleted"
    console.print(f"{mode}: {len(outcome.deleted)} file(s); {len(outcome.skipped)} skipped")


def _read_collection_results(input_dir: Path) -> list[CollectionResult]:
    return [
        CollectionResult.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(input_dir.glob("*.json"))
    ]


def main() -> None:
    """Entry point for the ``atlas`` console script."""
    app()


if __name__ == "__main__":
    main()

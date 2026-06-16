#!/usr/bin/env python3
"""
GoMining NFT Power Parser
=========================

Usage examples:

  # Check a single miner by ID:
  python main.py miner 349973

  # Check multiple miners:
  python main.py miner 349973 100001 200050 --token YOUR_GOMINING_JWT

  # Load IDs from a file (one per line):
  python main.py miner --file ids.txt

  # Scan all active listings in Release 2 collection, sort by TH/TON value:
  python main.py scan "GoMining Digital Miners: Release 2" --sort value --top 50

  # Same but only show upgraded miners (real > baseline power):
  python main.py scan "GoMining Digital Miners: Release 2" --upgraded-only

  # Export results to CSV:
  python main.py scan --all --csv output.csv

  # List supported collections:
  python main.py collections
"""

import os
import sys
import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from config import COLLECTIONS
from models import MinerInfo
from parser import fetch_single_miner, fetch_miners_parallel, fetch_collection_sales
from clients.gomining import GoMiningClient
from display import print_miners_table, print_single_miner, export_csv, console


# ── Shared options ────────────────────────────────────────────────────────────

def add_common_options(f):
    f = click.option(
        "--token", "-t",
        envvar="GOMINING_TOKEN",
        default=None,
        metavar="JWT",
        help="GoMining auth token (from browser DevTools). "
             "Enables real power lookup. Also reads GOMINING_TOKEN env var.",
    )(f)
    f = click.option(
        "--tonapi-key", "-k",
        envvar="TONAPI_KEY",
        default=None,
        metavar="KEY",
        help="tonapi.io API key (free at tonapi.io). Improves NFT address resolution.",
    )(f)
    return f


# ── CLI root ──────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """GoMining NFT Power Parser — find undervalued upgraded miners."""


# ── `collections` command ─────────────────────────────────────────────────────

@cli.command("collections")
def cmd_collections():
    """List supported GoMining NFT collections."""
    console.print("\n[bold]Supported GoMining NFT Collections:[/bold]\n")
    for name, addr in COLLECTIONS.items():
        console.print(f"  [cyan]{name}[/cyan]")
        console.print(f"    Address: [dim]{addr}[/dim]\n")


# ── `miner` command ───────────────────────────────────────────────────────────

@cli.command("miner")
@click.argument("indices", nargs=-1, type=int)
@click.option("--file", "-f", "ids_file", type=click.Path(exists=True),
              help="Text file with one miner ID per line.")
@click.option("--collection", "-c", default=None,
              help="Force collection name (auto-detected by default).")
@click.option("--workers", "-w", default=5, show_default=True,
              help="Parallel worker threads.")
@click.option("--csv", "csv_path", default=None, metavar="FILE",
              help="Export results to CSV file.")
@click.option("--sort", default="value",
              type=click.Choice(["value", "power", "upgrade", "price", "index"]),
              help="Sort results by column.")
@add_common_options
def cmd_miner(indices, ids_file, collection, workers, csv_path, sort, token, tonapi_key):
    """Check real power for specific miner ID(s).

    INDICES: one or more miner sequential numbers (e.g. 349973).
    """
    # Build ID list
    all_ids = list(indices)
    if ids_file:
        with open(ids_file) as fh:
            for line in fh:
                line = line.strip()
                if line and line.isdigit():
                    all_ids.append(int(line))

    if not all_ids:
        console.print("[red]No miner IDs provided. Pass IDs as arguments or use --file.[/red]")
        sys.exit(1)

    console.print(f"\n[bold]Fetching data for {len(all_ids)} miner(s)...[/bold]")

    gm_client = GoMiningClient(token) if token else None
    if not token:
        console.print(
            "[yellow]⚠  No --token provided. Real power from GoMining API will not be fetched.\n"
            "   Only blockchain baseline data will be shown.[/yellow]\n"
        )

    miners: list[MinerInfo] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Fetching...", total=len(all_ids))

        if len(all_ids) == 1:
            m = fetch_single_miner(
                all_ids[0], collection, gm_client, tonapi_key, verbose=True
            )
            miners = [m]
        else:
            def on_progress(done, total):
                progress.update(task, completed=done, total=total)

            miners = fetch_miners_parallel(
                all_ids, collection, gm_client, tonapi_key,
                max_workers=workers, progress_callback=on_progress,
            )

        progress.update(task, completed=len(all_ids))

    if len(miners) == 1:
        print_single_miner(miners[0])
    else:
        print_miners_table(miners, sort_by=sort)

    if csv_path:
        export_csv(miners, csv_path)


# ── `scan` command ────────────────────────────────────────────────────────────

@cli.command("scan")
@click.argument("collection_name", default="GoMining Digital Miners: Release 2")
@click.option("--all", "scan_all", is_flag=True,
              help="Scan both collections.")
@click.option("--max", "max_items", default=500, show_default=True,
              help="Max sale listings to fetch.")
@click.option("--top", "top_n", default=None, type=int,
              help="Show only top N results.")
@click.option("--sort", default="value",
              type=click.Choice(["value", "power", "upgrade", "price", "index"]),
              show_default=True, help="Sort results.")
@click.option("--upgraded-only", is_flag=True,
              help="Show only miners with real power > baseline (requires --token).")
@click.option("--min-power", default=None, type=float, metavar="TH",
              help="Filter: minimum TH/s power.")
@click.option("--max-price", default=None, type=float, metavar="TON",
              help="Filter: maximum price in TON.")
@click.option("--csv", "csv_path", default=None, metavar="FILE",
              help="Export results to CSV.")
@add_common_options
def cmd_scan(
    collection_name, scan_all, max_items, top_n, sort,
    upgraded_only, min_power, max_price, csv_path, token, tonapi_key,
):
    """Scan active sale listings in a collection and rank them.

    COLLECTION_NAME: "GoMining Digital Miners" or "GoMining Digital Miners: Release 2"
    """
    gm_client = GoMiningClient(token) if token else None
    if not token and upgraded_only:
        console.print(
            "[yellow]⚠  --upgraded-only requires --token to fetch real power from GoMining API.[/yellow]\n"
        )

    collections_to_scan = list(COLLECTIONS.keys()) if scan_all else [collection_name]

    all_miners: list[MinerInfo] = []

    for col in collections_to_scan:
        if col not in COLLECTIONS:
            console.print(f"[red]Unknown collection: {col!r}[/red]")
            console.print(f"Available: {list(COLLECTIONS.keys())}")
            sys.exit(1)

        console.print(f"\n[bold]Scanning:[/bold] [cyan]{col}[/cyan]")
        console.print(f"[dim]Fetching up to {max_items} sale listings...[/dim]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Scanning marketplace...", total=None)

            def on_progress(done, total):
                if total:
                    progress.update(task, completed=done, total=total)

            miners = fetch_collection_sales(
                col, gm_client, max_items=max_items, progress_callback=on_progress,
            )

        all_miners.extend(miners)
        console.print(f"[green]✓[/green] Found {len(miners)} listings")

    if top_n:
        # Sort first, then slice
        key_map = {
            "value": lambda m: -(m.th_per_ton or 0),
            "power": lambda m: -(m.real_power_th or m.baseline_power_th or 0),
            "upgrade": lambda m: -(m.upgrade_ratio or 0),
            "price": lambda m: (m.price_ton or float("inf")),
            "index": lambda m: m.index,
        }
        all_miners.sort(key=key_map.get(sort, key_map["value"]))
        all_miners = all_miners[:top_n]

    print_miners_table(
        all_miners,
        sort_by=sort,
        show_only_upgraded=upgraded_only,
        min_power=min_power,
        max_price=max_price,
    )

    if csv_path:
        export_csv(all_miners, csv_path)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()

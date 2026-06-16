"""Terminal output rendering using rich."""

from typing import Optional
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text
from rich.panel import Panel

from models import MinerInfo

console = Console()


def _fmt_power(th: Optional[float]) -> str:
    if th is None:
        return "[dim]—[/dim]"
    return f"[bold cyan]{th:.3f}[/bold cyan] TH"


def _fmt_price(ton: Optional[float]) -> str:
    if ton is None:
        return "[dim]—[/dim]"
    return f"[bold yellow]{ton:.2f}[/bold yellow] TON"


def _fmt_ratio(ratio: Optional[float]) -> str:
    if ratio is None:
        return "[dim]—[/dim]"
    pct = (ratio - 1) * 100
    if pct > 0:
        return f"[bold green]+{pct:.1f}%[/bold green]"
    elif pct < 0:
        return f"[bold red]{pct:.1f}%[/bold red]"
    return f"[dim]0%[/dim]"


def _fmt_value(th_per_ton: Optional[float]) -> str:
    if th_per_ton is None:
        return "[dim]—[/dim]"
    return f"[bold magenta]{th_per_ton:.4f}[/bold magenta]"


def _fmt_eff(wth: Optional[float]) -> str:
    if wth is None:
        return "[dim]—[/dim]"
    return f"{wth:.1f} W/TH"


def print_miners_table(
    miners: list[MinerInfo],
    sort_by: str = "value",
    show_only_upgraded: bool = False,
    min_power: Optional[float] = None,
    max_price: Optional[float] = None,
):
    """Print a rich table of miner data."""
    if not miners:
        console.print("[yellow]No miners to display.[/yellow]")
        return

    # Filter
    filtered = miners
    if show_only_upgraded:
        filtered = [m for m in filtered if m.upgrade_ratio and m.upgrade_ratio > 1.001]
    if min_power is not None:
        filtered = [
            m for m in filtered
            if (m.real_power_th or m.baseline_power_th or 0) >= min_power
        ]
    if max_price is not None:
        filtered = [m for m in filtered if m.price_ton and m.price_ton <= max_price]

    if not filtered:
        console.print("[yellow]No miners match the filter criteria.[/yellow]")
        return

    # Sort
    def sort_key(m: MinerInfo):
        if sort_by == "value":
            return -(m.th_per_ton or 0)
        elif sort_by == "upgrade":
            return -(m.upgrade_ratio or 0)
        elif sort_by == "power":
            return -(m.real_power_th or m.baseline_power_th or 0)
        elif sort_by == "price":
            return m.price_ton or float("inf")
        elif sort_by == "index":
            return m.index
        return -(m.th_per_ton or 0)

    filtered.sort(key=sort_key)

    has_real_power = any(m.real_power_th is not None for m in filtered)
    has_prices = any(m.price_ton is not None for m in filtered)

    table = Table(
        title="🔥 GoMining NFT Miner Analysis",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold white on dark_blue",
    )

    table.add_column("#", justify="right", style="dim", width=8)
    table.add_column("Name", min_width=22)
    table.add_column("Collection", min_width=14)
    table.add_column("Baseline\nPower", justify="center", min_width=12)

    if has_real_power:
        table.add_column("Real\nPower", justify="center", min_width=12)
        table.add_column("Upgrade\n+TH / Ratio", justify="center", min_width=14)

    if has_prices:
        table.add_column("Price\n(TON)", justify="right", min_width=12)
        table.add_column("Value\n(TH/TON)", justify="right", min_width=12)

    table.add_column("Efficiency", justify="center", min_width=10)
    table.add_column("On\nSale?", justify="center", width=6)

    for i, m in enumerate(filtered, 1):
        col_name = m.collection_name or "Unknown"
        short_col = col_name.replace("GoMining Digital Miners", "GDM").replace(": Release 2", " R2")

        row = [
            str(m.index),
            m.name or f"#{m.index}",
            short_col,
            _fmt_power(m.baseline_power_th),
        ]

        if has_real_power:
            row.append(_fmt_power(m.real_power_th))
            if m.real_power_th and m.baseline_power_th:
                added = m.added_power_th or 0
                ratio_str = _fmt_ratio(m.upgrade_ratio)
                row.append(f"+{added:.3f} TH\n{ratio_str}")
            else:
                row.append("[dim]—[/dim]")

        if has_prices:
            row.append(_fmt_price(m.price_ton))
            row.append(_fmt_value(m.th_per_ton))

        eff = m.real_efficiency_wth or m.baseline_efficiency_wth
        row.append(_fmt_eff(eff))
        row.append("[green]✓[/green]" if m.is_on_sale else "[dim]✗[/dim]")

        table.add_row(*row)

    console.print(table)
    console.print(f"\n[dim]Total: {len(filtered)} miners")

    if has_real_power:
        upgraded_count = sum(1 for m in filtered if m.upgrade_ratio and m.upgrade_ratio > 1.001)
        console.print(f"Upgraded miners (real > baseline): [green]{upgraded_count}[/green]")

    if has_prices:
        on_sale = [m for m in filtered if m.price_ton]
        if on_sale:
            avg_price = sum(m.price_ton for m in on_sale) / len(on_sale)
            console.print(f"Avg sale price: [yellow]{avg_price:.2f} TON[/yellow]")


def print_single_miner(m: MinerInfo):
    """Print detailed view of a single miner."""
    lines = []

    lines.append(f"[bold]{m.name}[/bold] ([dim]index: {m.index}[/dim])")
    if m.collection_name:
        lines.append(f"Collection: [cyan]{m.collection_name}[/cyan]")
    if m.address:
        lines.append(f"Address:    [dim]{m.address}[/dim]")

    lines.append("")
    lines.append("[bold underline]Power[/bold underline]")
    lines.append(f"  Baseline:  {_fmt_power(m.baseline_power_th)}")
    if m.real_power_th is not None:
        lines.append(f"  Real:      {_fmt_power(m.real_power_th)}")
        if m.added_power_th:
            lines.append(f"  Added:     [green]+{m.added_power_th:.3f} TH[/green] ({_fmt_ratio(m.upgrade_ratio)})")
    else:
        lines.append("  Real:      [dim]not available (GoMining token required)[/dim]")

    lines.append("")
    lines.append("[bold underline]Efficiency[/bold underline]")
    eff = m.real_efficiency_wth or m.baseline_efficiency_wth
    if eff:
        lines.append(f"  {eff:.1f} W/TH")
    else:
        lines.append("  [dim]—[/dim]")

    lines.append("")
    lines.append("[bold underline]Price[/bold underline]")
    if m.price_ton:
        lines.append(f"  {_fmt_price(m.price_ton)}")
        if m.th_per_ton:
            lines.append(f"  Value: {_fmt_value(m.th_per_ton)} TH per TON")
        if m.th_per_ton_baseline:
            lines.append(f"  Baseline value: {_fmt_value(m.th_per_ton_baseline)} TH per TON")
    elif m.is_on_sale:
        lines.append("  [yellow]On sale[/yellow] (price not retrieved)")
    else:
        lines.append("  [dim]Not for sale[/dim]")

    panel_text = "\n".join(lines)
    console.print(Panel(panel_text, title="Miner Details", expand=False))


def export_csv(miners: list[MinerInfo], path: str):
    """Export miner data to CSV."""
    import csv
    fieldnames = [
        "index", "name", "collection", "address",
        "baseline_power_th", "real_power_th", "added_power_th", "upgrade_pct",
        "price_ton", "th_per_ton", "efficiency_wth", "is_on_sale",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for m in miners:
            eff = m.real_efficiency_wth or m.baseline_efficiency_wth
            writer.writerow({
                "index": m.index,
                "name": m.name or "",
                "collection": m.collection_name or "",
                "address": m.address or "",
                "baseline_power_th": m.baseline_power_th or "",
                "real_power_th": m.real_power_th or "",
                "added_power_th": m.added_power_th or "",
                "upgrade_pct": f"{(m.upgrade_ratio - 1) * 100:.2f}" if m.upgrade_ratio else "",
                "price_ton": m.price_ton or "",
                "th_per_ton": m.th_per_ton or "",
                "efficiency_wth": eff or "",
                "is_on_sale": m.is_on_sale,
            })
    console.print(f"[green]Exported {len(miners)} miners to [bold]{path}[/bold][/green]")

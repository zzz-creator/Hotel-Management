# type: ignore
"""Console UI helpers built on top of `rich`.

All interactive menu/panel/table rendering for the hotel app goes here so that
the rest of the codebase stays free of raw `os.system` calls and repetitive
`logging.info` output.
"""
import os
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def clear_screen():
    """Clear the terminal on any platform (Win / Unix)."""
    os.system("cls" if os.name == "nt" else "clear")


def pause(message="Press Enter to continue..."):
    """Block until the user presses a key, on any platform."""
    if os.name == "nt":
        os.system("pause")
    else:
        input(message)


def info(message):
    """Print a plain informational line."""
    console.print(message)


def success(message):
    """Print a styled success message."""
    console.print(f"[bold green]{message}[/bold green]")


def error(message):
    """Print a styled error message."""
    console.print(f"[bold red]{message}[/bold red]")


def warning(message):
    """Print a styled warning message."""
    console.print(f"[bold yellow]{message}[/bold yellow]")


def box(title, content, border_style="green"):
    """Render `content` (multi-line string) inside a titled panel."""
    console.print(Panel(content, title=title, border_style=border_style))


def show_menu(title, lines):
    """Render a numbered menu inside a titled panel.

    `lines` is a list of strings. Strings of the form ``---- Section ----`` are
    rendered as section headers; ``N. Label`` lines are styled with a bold key.
    """
    rendered = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("----") and stripped.endswith("----"):
            section = stripped.strip("-").strip()
            rendered.append(f"[bold cyan]{section}[/bold cyan]")
        else:
            parts = line.split(". ", 1)
            if len(parts) == 2:
                rendered.append(f"[bold]{parts[0]}.[/bold] {parts[1]}")
            else:
                rendered.append(line)
    panel = Panel(
        "\n".join(rendered),
        title=title,
        border_style="cyan",
        padding=(0, 1),
    )
    console.print(panel)


def show_table(title, headers, rows):
    """Render tabular data with a styled title and header row."""
    table = Table(title=title, border_style="cyan", header_style="bold cyan")
    for header in headers:
        table.add_column(str(header), overflow="fold")
    for row in rows:
        table.add_row(*[str(value) for value in row])
    console.print(table)


# Room statuses and their rich colours (used by show_status_table).
STATUS_STYLES = {
    "Available": "green",
    "Occupied": "cyan",
    "Cleaning": "yellow",
    "Dirty": "red",
    "Maintenance": "magenta",
}


def show_status_table(title, headers, rows, status_col=2):
    """Like show_table but colour-codes the column holding a room status.

    `status_col` is the 0-based index of the status column (default 2).
    """
    table = Table(title=title, border_style="cyan", header_style="bold cyan")
    for header in headers:
        table.add_column(str(header), overflow="fold")
    for row in rows:
        rendered = [str(value) for value in row]
        status = rendered[status_col].strip()
        style = STATUS_STYLES.get(status)
        if style:
            rendered[status_col] = f"[{style}]{rendered[status_col]}[/{style}]"
        table.add_row(*rendered)
    console.print(table)


def ask_number(prompt, default=None, minimum=None, maximum=None):
    """Prompt for a number, validating the range. Returns an int."""
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw == "" and default is not None:
            return default
        try:
            value = int(raw)
        except ValueError:
            error("Invalid input. Please enter a number.")
            continue
        if minimum is not None and value < minimum:
            error(f"Value must be at least {minimum}.")
            continue
        if maximum is not None and value > maximum:
            error(f"Value must be at most {maximum}.")
            continue
        return value


def ask_confirmation(prompt, default="n"):
    """Prompt for a yes/no answer. Returns True/False."""
    raw = input(f"{prompt} (y/N): ").strip().lower()
    if raw == "":
        return default.lower() in ("y", "yes")
    return raw in ("y", "yes")


def ask_date(prompt, default=None):
    """Prompt for a date in YYYY-MM-DD format. Returns a `datetime.date`."""
    from datetime import date

    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw == "" and default:
            raw = default
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            error("Invalid date. Please use YYYY-MM-DD format.")


def ask_optional_date(prompt):
    """Prompt for a date in YYYY-MM-DD format, allowing a blank answer. Returns date or None."""
    while True:
        raw = input(f"{prompt} (blank to skip): ").strip()
        if raw == "":
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            error("Invalid date. Please use YYYY-MM-DD format.")
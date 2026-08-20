"""Turning what is on screen into a file somebody can keep.

Build Book: "one-click audit-ready bundle", and an export engine behind the
report catalogue. Until now the only thing that left this system as a file was
the bank payment instruction. An auditor asking for "that, as a file" had no
answer, which is an odd gap in a product whose whole argument is evidence.

Three formats, each for a different reader:

  * **CSV** — one table, for a spreadsheet. Deliberately one table per file
    rather than a sectioned document with blank lines between blocks: the
    sectioned kind looks tidy and stops being parseable by anything.
  * **HTML** — the whole report, readable, self-contained, and printable to
    PDF from any browser. No rendering dependency, which matters more than it
    sounds: a PDF library would be a third-party package in the path that
    produces audit evidence.
  * **JSON** — the canonical bundle, for a machine.

The rule that shapes the evidence pack export is that **the hash seals the
JSON, not the document**. A rendered page is a view of the bundle; re-hashing
the page gives a different number, and a PDF with a hash printed on it that
cannot be recomputed from the thing you are holding is decoration. So the HTML
export embeds the exact canonical JSON it was rendered from, and says how to
verify it. What you hold is checkable.
"""
import csv
import hashlib
import html
import io
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Excel and Google Sheets treat a cell opening with any of these as a formula.
#: A vendor named `=1+1` is a curiosity; one named
#: `=HYPERLINK("http://…"&A1)` exfiltrates the row it lands next to, and the
#: person who opens the file is an accountant who trusted an export from their
#: own finance system. The value is prefixed with an apostrophe, which Excel
#: strips on display — so the cell reads correctly and does not execute.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Written ahead of CSV output. Without it Excel on Windows reads UTF-8 as the
#: local codepage and mangles every non-ASCII vendor name — which is most of
#: them outside English. Parsers that dislike a BOM tolerate it far better than
#: a finance team tolerates corrupted names.
UTF8_BOM = "﻿"


def sanitize_cell(value: Any) -> str:
    """One CSV cell, safe to open in a spreadsheet."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"

    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def to_csv(columns: Sequence[str], rows: Sequence[Dict[str, Any]], *, bom: bool = True) -> str:
    """One table as RFC 4180 CSV.

    Column order is given rather than inferred from the first row, so a row
    that happens to be missing an optional key does not shift every later
    column by one — which is silent, and looks like bad data rather than a bad
    export.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([sanitize_cell(c) for c in columns])
    for row in rows:
        writer.writerow([sanitize_cell(row.get(c)) for c in columns])

    return (UTF8_BOM if bom else "") + buffer.getvalue()


def tables_in(payload: Dict[str, Any]) -> Dict[str, Tuple[List[str], List[Dict]]]:
    """Every table inside a report payload, by key.

    A dashboard is scalars plus lists of uniform dicts, so each such list is a
    table and everything else is the summary. Detected rather than declared:
    a new dashboard panel becomes exportable without anyone remembering to
    register it here, and a panel that stops returning rows exports as an
    empty table rather than disappearing.
    """
    found: Dict[str, Tuple[List[str], List[Dict]]] = {}

    for key, value in payload.items():
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(item, dict) for item in value):
            continue

        # Union of keys, first-seen order: rows in the same table occasionally
        # carry an extra field, and dropping it because row one lacked it
        # loses data silently.
        columns: List[str] = []
        for item in value:
            for column in item:
                if column not in columns:
                    columns.append(column)
        found[key] = (columns, value)

    return found


def summary_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The scalar figures, as name/value rows.

    Nested single dicts are flattened one level (`paid_last_30_days.runs`),
    which covers every shape the dashboards actually return without inventing
    a general tree-flattener nobody can predict the output of.
    """
    rows: List[Dict[str, Any]] = []

    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            rows.append({"measure": key, "value": value})
        elif isinstance(value, dict):
            for inner_key, inner_value in value.items():
                if isinstance(inner_value, (str, int, float, bool)) or inner_value is None:
                    rows.append({"measure": f"{key}.{inner_key}", "value": inner_value})

    return rows


def _humanise(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _cell_html(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return html.escape(str(value))


def to_html(
    *,
    title: str,
    subtitle: str = "",
    sections: Sequence[Tuple[str, Sequence[str], Sequence[Dict[str, Any]]]],
    meta: Optional[Dict[str, Any]] = None,
    embedded_json: Optional[str] = None,
    embedded_note: str = "",
) -> str:
    """A self-contained report document.

    Everything is inline — no stylesheet, no script, no font, nothing fetched.
    A document that phones home is not an archive: it renders differently in
    five years, or not at all, and an auditor's copy has to still open on a
    machine with no network.

    `embedded_json` is written verbatim into a script block so a reader can
    extract and re-hash exactly what was sealed. It is the only content not
    escaped as text, so it must be JSON the caller produced — never user input
    passed straight through.
    """
    parts: List[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;",
        "color:#111;background:#fff;margin:0;padding:2rem;max-width:70rem}",
        "h1{font-size:1.5rem;margin:0 0 .25rem}",
        "h2{font-size:1.05rem;margin:2rem 0 .5rem;border-bottom:1px solid #ddd;padding-bottom:.25rem}",
        ".sub{color:#555;margin:0 0 1.5rem}",
        "table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:13px}",
        "th,td{border:1px solid #ddd;padding:.4rem .55rem;text-align:left;vertical-align:top}",
        "th{background:#f5f5f5;font-weight:600}",
        "tr:nth-child(even) td{background:#fafafa}",
        ".meta{background:#f7f7f7;border:1px solid #e5e5e5;padding:.75rem 1rem;margin:0 0 1.5rem}",
        ".meta div{display:flex;gap:.5rem;padding:.1rem 0}",
        ".meta b{min-width:12rem;font-weight:600}",
        ".hash{font-family:ui-monospace,Consolas,monospace;word-break:break-all}",
        ".note{color:#555;font-size:12px;margin:1.5rem 0 0;border-top:1px solid #ddd;padding-top:.75rem}",
        ".empty{color:#777;font-style:italic}",
        "@media print{body{padding:0} h2{break-after:avoid} table{break-inside:auto}}",
        "</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
    ]

    if subtitle:
        parts.append(f'<p class="sub">{html.escape(subtitle)}</p>')

    if meta:
        parts.append('<div class="meta">')
        for key, value in meta.items():
            css = ' class="hash"' if "hash" in key.lower() else ""
            parts.append(
                f"<div><b>{html.escape(_humanise(key))}</b>"
                f"<span{css}>{_cell_html(value)}</span></div>"
            )
        parts.append("</div>")

    for name, columns, rows in sections:
        parts.append(f"<h2>{html.escape(name)}</h2>")
        if not rows:
            parts.append('<p class="empty">Nothing to report.</p>')
            continue
        parts.append("<table><thead><tr>")
        parts.extend(f"<th>{html.escape(_humanise(c))}</th>" for c in columns)
        parts.append("</tr></thead><tbody>")
        for row in rows:
            parts.append("<tr>")
            parts.extend(f"<td>{_cell_html(row.get(c))}</td>" for c in columns)
            parts.append("</tr>")
        parts.append("</tbody></table>")

    if embedded_json is not None:
        if embedded_note:
            parts.append(f'<p class="note">{html.escape(embedded_note)}</p>')
        # Escaping only `</` keeps a literal "</script>" inside a string value
        # from ending the block early. The JSON stays byte-identical for
        # hashing purposes once that sequence is reversed, and no realistic
        # audit payload contains it.
        safe = embedded_json.replace("</", "<\\/")
        parts.append(
            '<script type="application/json" id="canonical-bundle">'
            f"{safe}</script>"
        )

    parts.append("</body></html>")
    return "".join(parts)


def canonical_json(payload: Any) -> str:
    """The exact serialisation the pack hash is computed over.

    Shared with the evidence pack service on purpose. If these two ever
    disagree about separators or key order, every exported document claims a
    hash that cannot be reproduced from it — and nothing fails, it just stops
    being evidence.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

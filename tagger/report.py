"""Informe CSV y resumen por consola."""

import csv
import sys
from collections import Counter

COLUMNS = ["archivo", "campo", "actual", "propuesto", "fuente", "confianza", "nota"]


def setup_console():
    """La consola de Windows suele ser cp1252 y revienta con acentos."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


class Report:
    def __init__(self, csv_path):
        self.csv_path = csv_path
        self.rows = []
        self.stats = Counter()

    def add(self, path, proposals, note=""):
        self.stats["archivos"] += 1
        if note:
            self.stats["nota:" + note] += 1
        if not proposals:
            self.stats["sin_cambios"] += 1
            return
        self.stats["con_cambios"] += 1
        for proposal in proposals:
            self.stats["campo:" + proposal.field] += 1
            self.stats["fuente:" + proposal.source] += 1
            self.stats["conf:" + proposal.confidence] += 1
            self.rows.append([
                str(path), proposal.field, proposal.current, proposal.new,
                proposal.source, proposal.confidence, note,
            ])

    def save(self):
        if not self.rows:
            return None
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(COLUMNS)
            writer.writerows(self.rows)
        return self.csv_path

    def summary(self):
        stats = self.stats
        lines = [
            "",
            "=" * 66,
            "RESUMEN",
            "=" * 66,
            "  Archivos analizados : %d" % stats["archivos"],
            "  Con cambios         : %d" % stats["con_cambios"],
            "  Sin cambios         : %d" % stats["sin_cambios"],
            "  Cambios propuestos  : %d" % len(self.rows),
        ]

        def block(title, prefix):
            items = sorted(
                ((k.split(":", 1)[1], v) for k, v in stats.items() if k.startswith(prefix)),
                key=lambda kv: -kv[1],
            )
            if items:
                lines.append("")
                lines.append("  " + title)
                for name, count in items:
                    lines.append("    %-28s %6d" % (name, count))

        block("Por campo", "campo:")
        block("Por fuente", "fuente:")
        block("Por confianza", "conf:")
        block("Avisos", "nota:")
        lines.append("=" * 66)
        return "\n".join(lines)

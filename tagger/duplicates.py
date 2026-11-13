"""Deteccion de duplicados y traslado a cuarentena.

Dos archivos solo son duplicados si coinciden artista y titulo limpios Y ademas
duran practicamente lo mismo. La duracion es imprescindible: sin ella, una
version acelerada ("BABYDOLL (Speed)") o dos temas distintos del mismo sello se
agrupan como si fueran el mismo archivo.
"""

import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .tags import read_audio_info, read_tags
from .text import clean_pair, is_placeholder, norm

QUARANTINE = "_duplicados"

# Margen de duracion para considerar dos pistas la misma grabacion.
DURATION_ABS = 3.0
DURATION_REL = 0.02


def _key(path):
    tags = read_tags(path)
    if tags is None:
        return None, None
    artist = "" if is_placeholder("artist", tags["artist"]) else tags["artist"]
    title = "" if is_placeholder("title", tags["title"]) else tags["title"]
    clean_artist, clean_title = clean_pair(artist, title)
    if not clean_title:
        clean_title = path.stem
    return (norm(clean_artist), norm(clean_title)), tags


def _same_length(a, b):
    if not a or not b:
        return False
    tolerance = max(DURATION_ABS, DURATION_REL * max(a, b))
    return abs(a - b) <= tolerance


def _split_by_duration(entries):
    """Parte un grupo con la misma clave en subgrupos que si duran lo mismo."""
    groups = []
    for entry in sorted(entries, key=lambda e: e["duration"]):
        for group in groups:
            if _same_length(group[0]["duration"], entry["duration"]):
                group.append(entry)
                break
        else:
            groups.append([entry])
    return groups


def _quality(entry):
    """Mayor es mejor: portada, bitrate, tags rellenos, tamano."""
    tags = entry["tags"] or {}
    filled = sum(1 for f in ("title", "artist", "album", "genre", "date")
                 if tags.get(f) and not is_placeholder(f, tags.get(f, "")))
    return (
        1 if entry["cover_bytes"] > 1024 else 0,
        filled,
        entry["bitrate"],
        entry["size"],
    )


def find_duplicates(files, root, progress=None):
    """Devuelve la lista de grupos de duplicados, cada uno ya ordenado.

    El primero de cada grupo es el que conviene conservar.
    """
    buckets = defaultdict(list)
    for index, path in enumerate(files, start=1):
        if progress:
            progress(index)
        key, tags = _key(path)
        if key is None or not key[1]:
            continue
        info = read_audio_info(path)
        if info is None:
            continue
        buckets[key].append({
            "path": path,
            "tags": tags,
            "duration": info["duration"],
            "bitrate": info["bitrate"],
            "cover_bytes": info["cover_bytes"],
            "size": info["size"],
        })

    groups = []
    for key, entries in buckets.items():
        if len(entries) < 2:
            continue
        for subgroup in _split_by_duration(entries):
            if len(subgroup) < 2:
                continue
            subgroup.sort(key=_quality, reverse=True)
            groups.append({"key": key, "entries": subgroup})

    groups.sort(key=lambda g: (-len(g["entries"]), g["key"]))
    return groups


def why_keep(entry, others):
    reasons = []
    if entry["cover_bytes"] > 1024 and all(o["cover_bytes"] <= 1024 for o in others):
        reasons.append("unico con portada")
    if others and entry["bitrate"] > max(o["bitrate"] for o in others):
        reasons.append("mejor bitrate (%dk)" % (entry["bitrate"] // 1000))
    tags = entry["tags"] or {}
    filled = sum(1 for f in ("title", "artist", "album", "genre", "date")
                 if tags.get(f) and not is_placeholder(f, tags.get(f, "")))
    best_other = max((sum(1 for f in ("title", "artist", "album", "genre", "date")
                          if (o["tags"] or {}).get(f)
                          and not is_placeholder(f, (o["tags"] or {}).get(f, "")))
                      for o in others), default=0)
    if filled > best_other:
        reasons.append("mas tags (%d)" % filled)
    if not reasons:
        if others and all(o["size"] == entry["size"] for o in others):
            reasons.append("copias identicas, se conserva la primera")
        else:
            reasons.append("mayor tamano")
    return ", ".join(reasons)


def move_duplicates(groups, root, log_dir):
    """Mueve a cuarentena todos menos el mejor de cada grupo.

    No borra nada. Devuelve (movidos, ruta_del_log, fallos).
    """
    quarantine = Path(root) / QUARANTINE
    moves, failures = [], []

    for group in groups:
        for entry in group["entries"][1:]:
            source = entry["path"]
            try:
                relative = source.relative_to(root)
            except ValueError:
                relative = Path(source.name)
            target = quarantine / relative
            target.parent.mkdir(parents=True, exist_ok=True)

            counter = 2
            while target.exists():
                target = target.with_name("%s (%d)%s" % (target.stem, counter, target.suffix))
                counter += 1

            try:
                shutil.move(str(source), str(target))
                moves.append({"from": str(source), "to": str(target)})
            except (OSError, shutil.Error) as error:
                failures.append((str(source), str(error)))

    log_path = None
    if moves:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / ("movidos-%s.json" % stamp)
        with open(log_path, "w", encoding="utf-8") as handle:
            json.dump({"created": stamp, "kind": "move", "moves": moves},
                      handle, ensure_ascii=False, indent=1)

    return len(moves), log_path, failures

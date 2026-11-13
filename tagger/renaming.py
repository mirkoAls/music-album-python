"""Limpieza de caracteres raros en los nombres de archivo.

Conserva el nombre que ya tienes; solo retira lo que estorba: emoji, simbolos
decorativos, comillas tipograficas, caracteres de ancho completo, spam de webs
de descarga y los espacios o puntos finales que Windows no admite.
"""

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from .text import strip_noise, strip_video_id

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2B00-\u2BFF\u2700-\u27BF\uFE0F\u200B-\u200F\uFEFF]"
)

# Simbolos decorativos que aparecen en nombres de descargas.
DECORATIVE_RE = re.compile(r"[\u203B\u2605\u2606\u266A-\u266F\u25A0-\u25FF\u2022\u00B7\u2E3A\u2E3B]")

CJK_BRACKETS_RE = re.compile(r"[\uFF5F-\uFF60\u300C-\u300F\u3010\u3011\uFF62\uFF63]")

QUOTE_MAP = {
    "\u201C": "", "\u201D": "", "\u201E": "", "\u201F": "",
    "\u00AB": "", "\u00BB": "", "\u2033": "",
    "\u2018": "'", "\u2019": "'", "\u201A": "'", "\u00B4": "'", "\u0060": "'",
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2026": "...",
}

SPAM_RES = [
    re.compile(r"[\[\(_\-\s]*\d{2,3}\s*kbps\b[\]\)]*", re.IGNORECASE),
    re.compile(r"\bwww\.[\w\-]+\.\w{2,4}\b", re.IGNORECASE),
    re.compile(r"\b[\w\-]+\.(?:com|net|org|info|biz|ru|io|app)\b", re.IGNORECASE),
    re.compile(r"[\[\(]\s*(?:youtube|yt|descargar|descarga|download|free)\s*[\]\)]", re.IGNORECASE),
    re.compile(r"[\[\(]\s*mp3\s*[\]\)]", re.IGNORECASE),
]

WINDOWS_INVALID_RE = re.compile(r'[<>:"/\|?*\x00-\x1f]')

RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} \
    | {"lpt%d" % i for i in range(1, 10)}


def clean_name(stem):
    """Devuelve el nombre limpio (sin extension)."""
    text = strip_video_id(unicodedata.normalize("NFKC", stem))
    text = EMOJI_RE.sub("", text)
    text = DECORATIVE_RE.sub("", text)
    text = CJK_BRACKETS_RE.sub(" ", text)
    for source, target in QUOTE_MAP.items():
        text = text.replace(source, target)
    for pattern in SPAM_RES:
        text = pattern.sub(" ", text)
    # Mismo ruido de descarga que se quita de los tags: (Official Video),
    # [Official Audio], (Lyric Video), (HD)... Se conservan (Remix), (Live),
    # (Radio Edit) y demas, que identifican grabaciones distintas.
    text = strip_noise(text)
    text = WINDOWS_INVALID_RE.sub(" ", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)
    text = re.sub(r"(?:\s*_\s*){2,}", " _ ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([,;])\s*", r"\1 ", text)
    text = text.strip(" .-_")

    if not text or text.lower() in RESERVED:
        return ""
    return text


def plan_renames(files):
    """Devuelve [(origen, destino)] solo para los que cambian."""
    planned = []
    taken = set()
    for path in files:
        cleaned = clean_name(path.stem)
        if not cleaned or cleaned == path.stem:
            continue
        target = path.with_name(cleaned + path.suffix)
        counter = 2
        while (target.exists() and target != path) or str(target).lower() in taken:
            target = path.with_name("%s (%d)%s" % (cleaned, counter, path.suffix))
            counter += 1
        taken.add(str(target).lower())
        planned.append((path, target))
    return planned


def apply_renames(planned, log_dir, on_rename=None):
    """Renombra y deja un log para poder deshacerlo.

    on_rename(origen, destino) se llama tras cada renombrado que sale bien: lo
    usa el registro de portadas para que su marca siga al archivo.
    """
    done, failures = [], []
    for source, target in planned:
        try:
            source.rename(target)
        except OSError as error:
            failures.append((str(source), str(error)))
            continue
        done.append({"from": str(source), "to": str(target)})
        if on_rename:
            on_rename(source, target)

    log_path = None
    if done:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / ("renombrados-%s.json" % stamp)
        with open(log_path, "w", encoding="utf-8") as handle:
            json.dump({"created": stamp, "kind": "rename", "moves": done},
                      handle, ensure_ascii=False, indent=1)
    return len(done), log_path, failures


def undo(log_path, on_rename=None):
    """Deshace un log de renombrados o de traslados a cuarentena."""
    with open(log_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    restored, failures = 0, []
    for move in reversed(payload.get("moves", [])):
        source, target = Path(move["to"]), Path(move["from"])
        if not source.exists():
            failures.append((move["to"], "ya no existe"))
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        except OSError as error:
            failures.append((move["to"], str(error)))
            continue
        restored += 1
        if on_rename:
            on_rename(source, target)
    return restored, failures

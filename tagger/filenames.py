"""Extraccion de artista/titulo desde el nombre de archivo y las carpetas."""

import re

from .text import (clean_pair, collapse, is_placeholder, norm, overlap,
                   strip_noise, strip_video_id, tokens)

# Windows sustituye los caracteres invalidos del nombre original. Se conserva el
# espaciado: " _ " era " / " y separa artista de titulo, mientras que "km_h" era
# "km/h" y va dentro de la misma palabra.
# Basta con que haya espacio a UN lado: "Tei_ Napalm" tambien era una barra.
UNDERSCORE_SPACED_RE = re.compile(r"(?<=\S)(?:\s+_\s*|\s*_\s+)(?=\S)")
UNDERSCORE_TIGHT_RE = re.compile(r"(?<=\S)_(?=\S)")

# Solo se retira un numero inicial si lleva puntuacion detras. Asi "7 Years",
# "16 Shots" o "3 Doors Down" quedan intactos, y "14. Mi Kryptonita" no.
TRACK_PREFIX_RE = re.compile(r"^\s*\d{1,3}\s*[\.\)\-\u2013]\s+")
TRACK_PREFIX_PADDED_RE = re.compile(r"^\s*(\d{2,3})\s+(?=\S)")

SPLIT_RE = re.compile(r"\s+[-\u2013\u2014]\s+|\s*\|\s*|\s+/\s+")

# Nombres de carpeta que no identifican a un artista.
GENERIC_FOLDERS = {
    "musica", "music", "varios", "various", "descargas", "downloads",
    "nueva carpeta", "new folder", "canciones", "songs", "mp3", "audio",
}


def _looks_generic(name):
    key = norm(name)
    if not key or key in GENERIC_FOLDERS:
        return True
    if is_placeholder("artist", name) or is_placeholder("album", name):
        return True
    return bool(re.match(r"^(cd|disc|disco)\s*\d+$", key))


# Carpetas que estan al nivel de las de artista pero no son de un artista:
# "latino/brasil", "folklore/chacarera", "pop/70-80-90". Por fuera se ven
# igual, y solo se distinguen mirando lo que tienen dentro, asi que la lista
# la calcula "artists.category_folders" con la biblioteca entera a la vista y
# la deja apuntada aqui.
_CATEGORY_FOLDERS = set()


def set_category_folders(names):
    """Registra que carpetas NO son de artista, aunque lo parezcan."""
    global _CATEGORY_FOLDERS
    _CATEGORY_FOLDERS = {norm(name) for name in names or () if name}


def category_folders():
    return set(_CATEGORY_FOLDERS)


def raw_artist_folder(path, root):
    """Carpeta que podria ser la del artista, sin comprobar si lo es."""
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return ""
    for name in reversed(parts[1:]):
        if not _looks_generic(name):
            return name
    return ""


def folder_hints(path, root):
    """Pistas de la ruta: carpeta de genero (nivel 1) y carpeta de artista.

    Sube desde el archivo saltando carpetas que no dicen nada ("Interprete
    desconocido", "Album desconocido (15-4-2020 ...)") y las que resultaron ser
    de categoria y no de artista: en "latino/brasil" no canta nadie que se
    llame Brasil, y dar esa carpeta por artista repartia el mismo autor y la
    misma portada entre cuarenta canciones distintas.
    """
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        parts = ()

    genre_folder = parts[0] if parts else ""
    artist_folder = raw_artist_folder(path, root)
    if norm(artist_folder) in _CATEGORY_FOLDERS:
        artist_folder = ""
    return {"genre_folder": genre_folder, "artist_folder": artist_folder}


# Nombre en forma de slug: todo en minusculas, sin un solo espacio y con varios
# guiones. "pedro-fernandez-yo-soy-el-aventurero". Ahi los guiones son espacios,
# no separadores de artista y titulo.
SLUG_RE = re.compile(r"^[^\s]+$")


def _unslug(stem):
    if not SLUG_RE.match(stem) or stem.count("-") < 2:
        return stem
    return stem.replace("-", " ")


def clean_stem(path):
    stem = UNDERSCORE_SPACED_RE.sub(" / ", strip_video_id(path.stem))
    stem = UNDERSCORE_TIGHT_RE.sub("/", stem)
    stem = TRACK_PREFIX_RE.sub("", stem)
    stem = TRACK_PREFIX_PADDED_RE.sub("", stem)
    return collapse(_unslug(stem))


def _drop_channel_tail(parts):
    """Quita el ultimo trozo del nombre si es un canal o un sello.

    "Earshot / Wait / Warner Vault" trae tres trozos porque el "_" del nombre
    era una "/". El tercero es de quien subio el video, no de la cancion. Solo
    se descarta si quedan al menos dos trozos: con "Napalm Records - Dragostea"
    no hay nada de sobra que tirar.
    """
    from .artists import CHANNEL_WORDS

    while len(parts) >= 3 and CHANNEL_WORDS.search(parts[-1]):
        parts = parts[:-1]
    return parts


def looks_like_channel(text):
    """Este texto nombra un canal o un sello, no una cancion?"""
    from .artists import CHANNEL_WORDS

    limpio = collapse(text or "")
    if not limpio:
        return False
    return bool(CHANNEL_WORDS.search(limpio))


def parse_filename(path, artist_hint=""):
    """Deduce (artista, titulo, confianza) del nombre de archivo.

    confianza: "alta" si una pista externa confirma que lado es el artista,
    "media" si se asume el orden dominante 'Artista - Titulo', "baja" si no hay
    separador y el nombre entero se toma como titulo.
    """
    stem = clean_stem(path)
    parts = [p.strip() for p in SPLIT_RE.split(stem) if p.strip()]
    parts = _drop_channel_tail(parts)

    if len(parts) < 2:
        title = strip_noise(stem)
        return "", title, "baja"

    left = parts[0]
    right = " - ".join(parts[1:])

    if artist_hint:
        left_score = overlap(left, artist_hint)
        right_score = overlap(right, artist_hint)
        if right_score > left_score and right_score >= 0.6:
            artist, title = clean_pair(right, left)
            return artist, title, "alta"
        if left_score >= 0.6:
            artist, title = clean_pair(left, right)
            return artist, title, "alta"

    artist, title = clean_pair(left, right)
    return artist, title, "media"


def is_unrecoverable(path):
    """Pistas de rip sin informacion: '01 Pista 1', 'Track 05', 'Audio Track 3'."""
    stem = norm(path.stem)
    return bool(re.match(r"^\d{0,3}\s*(pista|track|audiotrack|audio track)\s*\d+$", stem))

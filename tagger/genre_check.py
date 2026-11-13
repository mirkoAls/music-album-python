"""Contrasta el genero que trae el archivo con la carpeta donde esta.

La fuente independiente es el tag de genero que NO es placeholder: en esta
biblioteca suele venir del sitio de descarga y es razonablemente fiable
("Dance/Electronic", "Hip-Hop/Rap", "Regional Mexican"). Solo informa; no mueve
ningun archivo.
"""

import re

from .text import is_placeholder, norm

# Vocabulario de los tags -> genero canonico.
KEYWORD_CANON = [
    ("reggaeton", "Reggaeton"), ("regueton", "Reggaeton"), ("urbano", "Reggaeton"),
    ("dancehall", "Reggae"), ("reggae", "Reggae"),
    ("trap", "Trap"),
    ("hip hop", "Hip-Hop"), ("hiphop", "Hip-Hop"), ("rap", "Hip-Hop"),
    ("bachata", "Bachata"),
    ("cumbia", "Cumbia"),
    ("salsa", "Salsa"), ("merengue", "Salsa"), ("tropical", "Salsa"),
    ("vallenato", "Vallenato"),
    ("regional mexican", "Regional"), ("banda", "Regional"),
    ("mariachi", "Regional"), ("ranchera", "Regional"), ("corrido", "Regional"),
    ("folklore", "Folklore"), ("folclore", "Folklore"), ("folk", "Folklore"),
    ("andina", "Folklore"), ("andean", "Folklore"),
    ("house", "Electronic"), ("techno", "Electronic"), ("trance", "Electronic"),
    ("edm", "Electronic"), ("dubstep", "Electronic"), ("dance", "Electronic"),
    ("electro", "Electronic"), ("phonk", "Electronic"), ("drum and bass", "Electronic"),
    ("metal", "Metal"),
    ("punk", "Rock"), ("grunge", "Rock"), ("alternativ", "Rock"),
    ("independiente", "Rock"), ("indie", "Rock"), ("rock", "Rock"),
    ("jazz", "Jazz"), ("blues", "Blues"), ("soul", "Soul"), ("funk", "Funk"),
    ("disco", "Disco"), ("country", "Country"), ("classical", "Classical"),
    ("clasica", "Classical"), ("soundtrack", "Soundtrack"), ("anime", "Soundtrack"),
    ("latin", "Latin"), ("latino", "Latin"),
    ("pop", "Pop"),
]

SPLIT_RE = re.compile(r"[,/;&|]|\s+y\s+")

# Que generos son coherentes con cada carpeta.
FOLDER_ACCEPTS = {
    "pop": {"Pop", "Rock", "Electronic", "Soul", "Disco", "Soundtrack"},
    "rock": {"Rock", "Metal", "Pop", "Soundtrack"},
    "electro": {"Electronic", "Pop", "Disco"},
    "trap": {"Trap", "Hip-Hop", "Reggaeton"},
    "hiphop-rap": {"Hip-Hop", "Trap", "Reggaeton", "Soul", "Funk"},
    "regueton": {"Reggaeton", "Trap", "Latin", "Hip-Hop"},
    "regue": {"Reggae", "Latin"},
    "bachata": {"Bachata", "Salsa", "Latin"},
    "cumbia": {"Cumbia", "Salsa", "Latin", "Folklore", "Vallenato"},
    "folklore": {"Folklore", "Latin", "Vallenato", "Regional"},
    "latino": {"Latin", "Pop", "Salsa", "Bachata", "Cumbia", "Reggaeton",
               "Regional", "Vallenato", "Folklore", "Rock"},
}

CANON_TO_FOLDER = {
    "Reggaeton": "regueton", "Reggae": "regue", "Trap": "trap",
    "Hip-Hop": "hiphop-rap", "Bachata": "bachata", "Cumbia": "cumbia",
    "Salsa": "latino", "Vallenato": "cumbia", "Regional": "latino",
    "Folklore": "folklore", "Electronic": "electro", "Metal": "rock",
    "Rock": "rock", "Pop": "pop", "Latin": "latino",
}

# Generos paraguas: son la etiqueta por defecto de los sitios de descarga y no
# contradicen a ninguna carpeta. Bad Bunny, J Balvin o Alexis y Fido vienen
# marcados como "Pop", y tratarlo como prueba de mala ubicacion generaba 179
# falsas alarmas de 318.
VAGUE = {"Pop", "Latin"}

# Carpetas de idioma o cajon de sastre: el genero no dice nada sobre si el
# archivo esta bien colocado.
IGNORED_FOLDERS = {"aleman", "frances", "ruso", "ramdom", "random", "ingles",
                   "italiano", "portugues", "japones", "coreano"}


def canonical_genres(raw_genre):
    """Traduce el texto del tag a un conjunto de generos canonicos."""
    if not raw_genre or is_placeholder("genre", raw_genre):
        return set()
    found = set()
    for chunk in SPLIT_RE.split(raw_genre):
        key = norm(chunk)
        if not key:
            continue
        for needle, canon in KEYWORD_CANON:
            if needle in key:
                found.add(canon)
                break
    return found


def check(path, root, raw_genre):
    """Devuelve None si encaja, o un dict describiendo el desajuste."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if not parts:
        return None

    folder = parts[0]
    key = norm(folder)
    if key in IGNORED_FOLDERS:
        return None

    accepted = FOLDER_ACCEPTS.get(key.replace(" ", "-"))
    if accepted is None:
        accepted = FOLDER_ACCEPTS.get(key)
    if accepted is None:
        return None

    canon = canonical_genres(raw_genre)
    if not canon or canon & accepted:
        return None

    specific = canon - VAGUE
    if not specific:
        return None

    suggestions = sorted({CANON_TO_FOLDER[c] for c in specific if c in CANON_TO_FOLDER})
    return {
        "folder": folder,
        "genre": raw_genre,
        "canonical": ", ".join(sorted(specific)),
        "suggested": ", ".join(suggestions),
    }


# Nombre preferido para mostrar cada genero canonico.
CANON_DISPLAY = {
    "Reggaeton": "Reggaeton", "Reggae": "Reggae", "Trap": "Trap",
    "Hip-Hop": "Hip-Hop", "Bachata": "Bachata", "Cumbia": "Cumbia",
    "Salsa": "Salsa", "Vallenato": "Vallenato", "Regional": "Regional Mexican",
    "Folklore": "Folklore", "Electronic": "Electronic", "Metal": "Metal",
    "Rock": "Rock", "Pop": "Pop", "Latin": "Latin", "Jazz": "Jazz",
    "Blues": "Blues", "Soul": "Soul", "Funk": "Funk", "Disco": "Disco",
    "Country": "Country", "Classical": "Classical", "Soundtrack": "Soundtrack",
}

# Al elegir uno solo, el mas especifico primero.
CANON_PRIORITY = [
    "Reggaeton", "Trap", "Bachata", "Cumbia", "Vallenato", "Regional", "Salsa",
    "Folklore", "Reggae", "Hip-Hop", "Metal", "Electronic", "Jazz", "Blues",
    "Soul", "Funk", "Disco", "Country", "Classical", "Soundtrack", "Rock",
    "Latin", "Pop",
]


def genre_from_online(names):
    """Convierte los generos que devuelve la fuente en uno solo, canonico."""
    found = set()
    for name in names or []:
        found |= canonical_genres(name)
    # El "folk" de los catalogos es el anglosajon, no el folklore de esta
    # biblioteca. Si viene acompanado de otro genero manda el otro: Deezer da
    # "Alternativo, Indie Pop, Indie Pop/Folk" a un tema indie, y por ir
    # Folklore antes en la prioridad acababa de folklore.
    if "Folklore" in found and len(found) > 1 and not any(
            marca in norm(name) for name in names or []
            for marca in ("folklor", "folclor", "andin", "andean")):
        found.discard("Folklore")
    for canon in CANON_PRIORITY:
        if canon in found:
            return CANON_DISPLAY.get(canon, canon)
    return ""

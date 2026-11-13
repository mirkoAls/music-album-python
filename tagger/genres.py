"""Genero deducido de la carpeta de primer nivel.

Para esta biblioteca la carpeta es mejor fuente que MusicBrainz: MusicBrainz
devuelve generos escasos y a menudo vacios, mientras que la carpeta refleja una
clasificacion que ya hizo una persona.
"""

from .text import norm

# Carpetas que SI son un genero.
FOLDER_GENRES = {
    "pop": "Pop",
    "rock": "Rock",
    "trap": "Trap",
    "regueton": "Reggaeton",
    "reggaeton": "Reggaeton",
    "regue": "Reggae",
    "reggae": "Reggae",
    "bachata": "Bachata",
    "cumbia": "Cumbia",
    "folklore": "Folklore",
    "folclore": "Folklore",
    "hiphop rap": "Hip-Hop",
    "hiphop": "Hip-Hop",
    "rap": "Hip-Hop",
    "electro": "Electronic",
    "electronica": "Electronic",
    "latino": "Latin",
    "salsa": "Salsa",
    "merengue": "Merengue",
    "vallenato": "Vallenato",
    "metal": "Metal",
    "jazz": "Jazz",
    "blues": "Blues",
    "clasica": "Classical",
    "country": "Country",
    "punk": "Punk",
    "indie": "Indie",
    "disco": "Disco",
    "funk": "Funk",
    "soul": "Soul",
    "banda": "Banda",
    "corridos": "Corridos",
    "ranchera": "Ranchera",
    "tango": "Tango",
    "salsa choke": "Salsa",
}

# Carpetas que NO son genero: idioma, cajon de sastre, decadas.
NON_GENRE_FOLDERS = {
    "aleman", "frances", "ruso", "ingles", "italiano", "portugues", "japones",
    "coreano", "arabe", "espanol", "ramdom", "random", "varios", "otros",
    "mix", "playlist", "favoritos", "nuevas", "viejas",
}


def genre_for_folder(folder_name):
    """Devuelve el genero de la carpeta, o "" si la carpeta no es un genero."""
    key = norm(folder_name)
    if not key or key in NON_GENRE_FOLDERS:
        return ""
    if key.isdigit() or key in {"70 80 90", "80s", "90s", "2000"}:
        return ""
    return FOLDER_GENRES.get(key, "")


# ---------------------------------------------------------------------------
# Genero deducido del propio artista
# ---------------------------------------------------------------------------

def genres_by_artist(files, min_votes=1):
    """Genero mas frecuente de cada artista, mirando la biblioteca entera.

    Un artista no cambia de genero entre canciones. Si "Apache 207 - Roller" ya
    tiene Hip-Hop, sus otros temas tambien lo son, y eso sale gratis sin
    consultar nada. Cubre lo que las fuentes online no dan: MusicBrainz acierta
    el album pero no devuelve genero.

    Basta con una cancion: exigir dos se probo y salia peor. Filtraba cuatro
    herencias correctas (Bausa, Capital Bra, H1, Young Mask) y no eliminaba el
    caso dudoso que pretendia evitar, porque ese artista ya tenia dos.
    """
    from collections import Counter, defaultdict

    from .tags import read_tags
    from .text import is_placeholder, norm

    votos = defaultdict(Counter)
    for path in files:
        tags = read_tags(path)
        if tags is None:
            continue
        artista = tags.get("artist", "")
        genero = tags.get("genre", "")
        if not artista or not genero:
            continue
        if is_placeholder("artist", artista) or is_placeholder("genre", genero):
            continue
        votos[norm(artista)][genero] += 1

    resultado = {}
    for clave, contador in votos.items():
        genero, cuenta = contador.most_common(1)[0]
        if cuenta >= min_votes:
            resultado[clave] = genero
    return resultado

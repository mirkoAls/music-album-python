"""Detecta tags de artista que estan rellenos pero mal.

El caso tipico es el nombre del canal de YouTube en vez del artista:
"xTheLYRICS" en un archivo llamado "Apache 207 - 200 km_h.mp3". No basta con
mirar si el tag esta vacio, porque no lo esta.

Hace falta ver la biblioteca entera antes de juzgar: un mismo tag que aparece
en canciones de artistas distintos es un canal o un sello, y eso solo se sabe
comparando unos archivos con otros.
"""

import re
from collections import Counter, defaultdict

from .filenames import SPLIT_RE, clean_stem
from .text import artist_names, clean_artist, is_placeholder, norm, overlap

# Palabras propias de canales y sellos, no de artistas.
CHANNEL_WORDS = re.compile(
    r"\b(?:records?|recordings?|music|musica|vibes|tv|hd|hq|official|lyrics?|"
    r"hits?|media|nation|channel|entertainment|studios?|topic|vevo|beats|"
    r"sounds?|radio|tube|promo|release|label|network|group)\b",
    re.IGNORECASE)

VERDICTS = ("formato", "canal", "dudoso")

# Carpetas tematicas que, dentro de esta biblioteca, viven al mismo nivel que
# las carpetas de artista. No basta con una lista cerrada: mas abajo tambien se
# usa la diversidad real de artistas para descubrir categorias nuevas. Esta
# lista cubre los casos ambiguos en que varias canciones ya heredaron por error
# el nombre de la carpeta y contaminaron la evidencia estadistica.
CATEGORY_FOLDER_NAMES = {
    "brasil", "brazil", "wwe", "salsa", "merengue", "salsa merengue",
    "vallenato", "chacarera", "cueca", "caporal", "caporales", "morenada",
    "tinku", "saya", "salay", "diablada", "tobas", "huayno", "taquirari",
    "carnavalito", "zamba", "chamame", "bolero", "anime", "openings",
}


def _folder_matches_artist(folder, artist):
    """La carpeta parece nombrar a este artista, incluso con una errata."""
    from .text import same_text

    return (overlap(folder, artist) >= 0.6
            or same_text(folder, artist, threshold=0.80))


def _obvious_category_name(name):
    key = norm(name)
    if key in CATEGORY_FOLDER_NAMES:
        return True
    # "70-80-90", "1980-1990" y variantes: son decadas, no interpretes.
    return bool(re.fullmatch(r"\d{2,4}(?:\s+\d{2,4}){1,4}", key))


def squash(value):
    """Sin espacios ni puntuacion: 'AC/DC' y 'ACDC' se vuelven iguales."""
    return re.sub(r"[^a-z0-9]", "", norm(value))


def _separators(value):
    return len(re.findall(r"[^\w]", value or ""))


def better_written(a, b):
    """De dos escrituras del mismo nombre, la que separa mejor las palabras."""
    if _separators(a) != _separators(b):
        return a if _separators(a) > _separators(b) else b
    return a if len(a) >= len(b) else b


def filename_artist(path):
    """Lado izquierdo del nombre de archivo, si lo hay."""
    parts = [p.strip() for p in SPLIT_RE.split(clean_stem(path)) if p.strip()]
    if len(parts) < 2:
        return "", ""
    return parts[0], " - ".join(parts[1:])


def collect(files, progress=None):
    """Primera pasada: reune tag y nombre de archivo de cada cancion."""
    from .tags import read_tags

    entries = []
    for index, path in enumerate(files, start=1):
        if progress:
            progress(index)
        raw = read_tags(path)
        if raw is None:
            continue
        tag = raw.get("artist", "")
        if not tag or is_placeholder("artist", tag):
            continue
        left, right = filename_artist(path)
        # Sin separador no hay artista que proponer, pero un tag generico como
        # "Release" sigue siendo una senal util: debe marcarse como canal para
        # que las busquedas usen solo el titulo en vez de exigir ese falso
        # artista. Era el caso de "nie yeliebt.mp3".
        # Se juzga el tag YA LIMPIO: en "Wisin - Topic" o "Zate Musik" lo que
        # sobra es el sufijo, y quitarlo deja al artista de verdad. Mirando el
        # crudo, el "topic" de los canales automaticos de YouTube condenaba a
        # "dudoso" a artistas perfectos y la busqueda se quedaba sin artista.
        limpio = clean_artist(tag)
        if not left and not CHANNEL_WORDS.search(limpio):
            continue
        entries.append({
            "path": path,
            "tag": limpio,
            "raw_tag": tag,
            "left": left,
            "right": right,
        })
    return entries


def judge(entries):
    """Segunda pasada: con la biblioteca entera a la vista, dictamina."""
    disagreeing = []
    for entry in entries:
        tag = entry["tag"]
        if overlap(entry["left"], tag) or overlap(entry["right"], tag):
            continue
        disagreeing.append(entry)

    # Un tag que acompana a varios artistas distintos es un canal o un sello.
    seen_with = defaultdict(set)
    for entry in disagreeing:
        seen_with[norm(entry["tag"])].add(norm(entry["left"]))

    findings = []
    for entry in disagreeing:
        tag, left = entry["tag"], entry["left"]
        squashed_tag = squash(tag)
        squashed_left = squash(left)

        if squashed_tag and squashed_left and (
                squashed_tag == squashed_left
                or squashed_tag in squashed_left
                or squashed_left in squashed_tag):
            best = better_written(tag, left)
            if best == tag:
                continue
            findings.append({
                "path": entry["path"], "verdict": "formato", "current": entry["raw_tag"],
                "suggested": best,
                "reason": "mismo nombre peor escrito en el tag",
            })
            continue

        distinct = len(seen_with[norm(tag)])
        if distinct >= 2:
            findings.append({
                "path": entry["path"], "verdict": "canal", "current": entry["raw_tag"],
                "suggested": left,
                "reason": "el tag acompana a %d artistas distintos" % distinct,
            })
            continue

        if CHANNEL_WORDS.search(tag):
            findings.append({
                "path": entry["path"], "verdict": "canal", "current": entry["raw_tag"],
                "suggested": left,
                "reason": "el tag parece un canal o un sello",
            })
            continue

        findings.append({
            "path": entry["path"], "verdict": "dudoso", "current": entry["raw_tag"],
            "suggested": left,
            "reason": "el tag no se parece al nombre del archivo",
        })

    return findings


def known_names(files, root, min_count=2):
    """Nombres que la biblioteca entera ya usa como artista.

    Es lo que permite decidir de que lado de "A - B" esta el artista cuando el
    tag no sirve y el nombre del archivo no lleva ninguna marca: si "Los
    Palmeras" aparece como artista en otros temas y "La cola" no, no hay duda de
    cual es cual.

    Un nombre entra si es la carpeta de un artista, o si lo llevan al menos dos
    canciones. Con una sola se colaria el nombre de cualquier canal.
    """
    from collections import Counter

    from .filenames import folder_hints
    from .tags import read_tags
    from .text import main_artist

    veces = Counter()
    carpetas = set()
    for path in files:
        carpeta = folder_hints(path, root)["artist_folder"]
        if carpeta:
            carpetas.add(norm(carpeta))
        raw = read_tags(path)
        if raw is None:
            continue
        tag = raw.get("artist", "")
        if not tag or is_placeholder("artist", tag):
            continue
        principal = norm(main_artist(clean_artist(tag)))
        if principal:
            veces[principal] += 1
    carpetas.discard("")
    return {nombre for nombre, cuantas in veces.items()
            if cuantas >= min_count} | carpetas


def canonical_artist_spellings(files, min_count=2):
    """Grafia dominante de cada artista, sin cambiar su identidad.

    Solo devuelve nombres respaldados por varias apariciones y con un ganador
    claro. Esto permite conservar BABYMETAL o LiL CaKe sin convertir una errata
    aislada —ni la escritura de una sola fuente— en regla para la biblioteca.
    """
    from .tags import read_tags

    variants = defaultdict(Counter)
    for path in files:
        raw = read_tags(path)
        if raw is None:
            continue
        seen = set()
        for field in ("artist", "albumartist"):
            credit = raw.get(field, "")
            if not credit or is_placeholder("artist", credit):
                continue
            for name in artist_names(clean_artist(credit)):
                canonical = norm(name)
                pair = (canonical, name)
                if canonical and pair not in seen:
                    variants[canonical][name] += 1
                    seen.add(pair)

    result = {}
    for canonical, spellings in variants.items():
        ranked = spellings.most_common()
        if not ranked or ranked[0][1] < min_count:
            continue
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            continue
        result[canonical] = ranked[0][0]
    return result


def canonical_album_spellings(files, min_count=2):
    """Grafia dominante de cada album, sin cambiar de disco.

    Solo agrupa titulos cuya forma normalizada es IDENTICA, asi que nunca junta
    dos discos distintos: lo unico que decide es como se escribe el que ya
    estaba. Por eso no hace falta separar por artista, y conviene no hacerlo,
    porque el mismo disco puede venir acreditado a "Kumbia All-Starz" en unos
    temas y a "A.B. Quintanilla" en otros.

    Sin esto, cada fuente escribe a su manera y el reproductor parte un disco en
    dos: cuatro canciones bajo "La Vida de un Genio" y una bajo "La vida de un
    genio".
    """
    from .tags import read_tags

    variantes = defaultdict(Counter)
    for path in files:
        raw = read_tags(path)
        if raw is None:
            continue
        titulo = (raw.get("album") or "").strip()
        if not titulo or is_placeholder("album", titulo):
            continue
        clave = norm(titulo)
        if clave:
            variantes[clave][titulo] += 1

    resultado = {}
    for clave, escrituras in variantes.items():
        ordenadas = escrituras.most_common()
        if len(ordenadas) < 2:
            continue
        if ordenadas[0][1] < min_count:
            continue
        # Empate: no hay una grafia dominante, se deja lo que haya.
        if ordenadas[0][1] == ordenadas[1][1]:
            continue
        resultado[clave] = ordenadas[0][0]
    return resultado


def category_folders(files, root, min_files=8, min_names=3, min_share=0.4):
    """Carpetas que parecen de artista y no lo son.

    Quien ordeno la biblioteca creo carpetas de pais, de ritmo o de decada al
    mismo nivel que las de artista: "latino/brasil", "folklore/chacarera",
    "pop/70-80-90". Desde fuera no hay forma de distinguirlas, y confundirlas
    sale caro: la carpeta se usa como artista de reserva y como clave para
    repartir portadas entre temas del mismo disco, asi que las noventa y una
    canciones de "brasil" acababan con el mismo autor y la misma caratula.

    La prueba es lo que hay dentro, no el nombre. En la carpeta de un artista
    ese artista aparece en casi todos los temas, aunque sea como invitado: en
    "Khalid" sale Khalid en los cinco, y en "bad bunny" en 122 de 173. En una
    carpeta de ritmo no se repite nadie: el nombre que mas sale en "brasil"
    cubre 12 de 91.

    Se cuentan TODOS los nombres del credito, no solo el principal. La carpeta
    de un rapero esta llena de "Otro feat. El", y mirando solo el primero
    parecia que ahi no cantaba nunca el dueno de la carpeta.

    Se exige un minimo de archivos y de nombres distintos: con seis canciones
    no hay con que juzgar, y en la duda se deja como estaba. Los nombres de
    CATEGORY_FOLDER_NAMES no necesitan esa prueba, porque son justo las
    carpetas donde pasadas anteriores ya escribieron el nombre de la carpeta
    como artista y falsearon el recuento.
    """
    from .filenames import raw_artist_folder
    from .tags import read_tags
    from .text import artist_names, same_text

    creditos = defaultdict(list)
    for path in files:
        carpeta = raw_artist_folder(path, root)
        if not carpeta:
            continue
        credito = ""
        raw = read_tags(path)
        if raw:
            tag = raw.get("artist", "")
            if tag and not is_placeholder("artist", tag):
                credito = clean_artist(tag)
        if not credito:
            izquierda, _ = filename_artist(path)
            credito = clean_artist(izquierda)
        nombres = {norm(x) for x in artist_names(credito)}
        nombres.discard("")
        creditos[carpeta].append(nombres)

    categorias = set()
    for carpeta, por_archivo in creditos.items():
        if _obvious_category_name(carpeta):
            categorias.add(carpeta)
            continue

        total = len(por_archivo)
        veces = Counter()
        for nombres in por_archivo:
            for nombre in nombres:
                veces[nombre] += 1
        if total < min_files or len(veces) < min_names:
            continue

        # El nombre que mas se repite, juntando sus variantes de escritura:
        # "Snoop Dogg" y "snoop dogg ice cube" son el mismo dueno de carpeta.
        mayor = 0
        for nombre in veces:
            juntos = sum(cuantas for otro, cuantas in veces.items()
                         if otro == nombre or same_text(otro, nombre))
            mayor = max(mayor, juntos)
        # Y si alguno de esos nombres ES la carpeta, es de artista aunque se
        # escriba con erratas: "marron 5" es Maroon 5.
        if any(_folder_matches_artist(carpeta, nombre) for nombre in veces):
            mayor = max(mayor, int(min_share * total))
        if mayor < min_share * total:
            categorias.add(carpeta)

    return categorias


def category_impostor_paths(files, root, categories=()):
    """Archivos cuyo artista es en realidad el nombre de una carpeta.

    Es un destrozo que hizo este mismo programa: al no encontrar artista usaba
    la carpeta como ultimo recurso, y en las carpetas que no son de nadie eso
    escribio "caporal" en treinta y dos canciones y "brasil" en doce. Ese
    nombre no vale ni como artista ni para buscar, asi que estos archivos se
    tratan igual que los que traen el nombre de un canal: manda el nombre del
    archivo.
    """
    from .tags import read_tags

    sospechosas = {norm(x) for x in categories if x}
    sospechosas.discard("")
    impostores = set()
    for path in files:
        raw = read_tags(path)
        if raw is None:
            continue
        tag = raw.get("artist", "")
        if not tag or is_placeholder("artist", tag):
            continue
        clave = norm(clean_artist(tag))
        if not clave:
            continue
        if clave in sospechosas or _obvious_category_name(clave):
            impostores.add(str(path))
            continue
        # Tambien la carpeta de primer nivel, que siempre es un genero:
        # un archivo con el artista "trap" o "cumbia" no tiene artista.
        try:
            primera = path.relative_to(root).parts[0]
        except (ValueError, IndexError):
            continue
        if len(path.relative_to(root).parts) > 1 and clave == norm(primera):
            impostores.add(str(path))
    return impostores


def suspect_paths(findings):
    """Rutas cuyo artista no es de fiar, para no propagarlo a albumartist."""
    return {str(f["path"]) for f in findings if f["verdict"] in ("canal", "dudoso")}

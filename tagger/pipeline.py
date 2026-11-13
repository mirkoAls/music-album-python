"""Orquestacion: limpieza local -> pistas de ruta -> MusicBrainz -> escritura."""

import re
from pathlib import Path

from .correcciones import CAMPOS_DE_TAG, compartidas
from .deezer import duration_tolerance
from .filenames import clean_stem, folder_hints, is_unrecoverable, parse_filename
from .genre_check import genre_from_online
from .genres import genre_for_folder
from .text import (SPLIT_RE, TIGHT_SPLIT_RE, artist_names, bare_title,
                   clean_artist, clean_date, clean_pair, clean_track_number,
                   collapse, is_placeholder, jaccard, key, main_artist, norm,
                   overlap, same_text, preserve_artist_case, split_pipe_artist,
                   split_title_pair, strip_artist_affix, strip_artist_prefix,
                   strip_noise, tokens)

TAG_FIELDS = ("title", "artist", "album", "genre", "date", "tracknumber")
CORRECTIONS_PATH = Path(__file__).resolve().parent.parent / "correcciones.json"

# Que confianzas se escriben con --apply.
WRITABLE = {"alta", "media"}
DOUBTFUL = {"revisar", "baja"}

VERSION_MARKER_RE = re.compile(
    r"\b(?:remix|mix|edit|live|acoustic|instrumental|extended|radio\s+edit|"
    r"version)\b", re.IGNORECASE)


def correction_for(path):
    """Lo decidido a mano para este archivo en correcciones.json, o {}."""
    return compartidas(CORRECTIONS_PATH).para(path)


def identity_correction(path):
    value = correction_for(path)
    artist = collapse(value.get("artist", ""))
    title = strip_noise(value.get("title", ""))
    return (artist, title) if artist and title else None


class Proposal:
    __slots__ = ("field", "current", "new", "source", "confidence")

    def __init__(self, field, current, new, source, confidence):
        self.field = field
        self.current = current
        self.new = new
        self.source = source
        self.confidence = confidence

    def writable(self, include_doubtful=False):
        if self.confidence in WRITABLE:
            return True
        return include_doubtful and self.confidence in DOUBTFUL


def _clean_album(raw_album, artist, title):
    if is_placeholder("album", raw_album):
        return ""
    album, _ = strip_artist_prefix(strip_noise(raw_album), artist)
    album = collapse(album)
    # Un "album" identico al titulo del video no aporta nada.
    if album and norm(album) == norm(strip_noise(raw_album)) and norm(album) == norm(title):
        return album
    return album


def _witnesses(raw, hints, stem):
    """Lo que dice del artista todo lo que NO es el campo de artista.

    El nombre del archivo, la carpeta y el albumartist son independientes del
    tag de artista, salvo cuando el albumartist es una copia de el (que es lo
    normal y entonces no atestigua nada).
    """
    from .artists import CHANNEL_WORDS
    from .text import PIPE_RE

    artist_raw = raw.get("artist", "")
    bruto = raw.get("albumartist", "")
    album_artist = "" if is_placeholder("artist", bruto) else clean_artist(bruto)
    # Un albumartist con barra o con palabra de canal no es un artista, es el
    # bloque que dejo el descargador: "Dragostea Din Tei (Video) | Napalm
    # Records" es el titulo mas el sello, y como testigo miente.
    if PIPE_RE.search(bruto) or CHANNEL_WORDS.search(bruto):
        album_artist = ""
    # Copiado del campo de artista: no atestigua nada.
    if album_artist and overlap(album_artist, artist_raw) >= 0.6:
        album_artist = ""
    return [x for x in (stem, hints.get("artist_folder", ""), album_artist) if x]


def online_artist_hints(raw, hints):
    """Artistas alternativos seguros para consultar en las fuentes.

    La carpeta y ``albumartist`` suelen saber mas que un tag contaminado por el
    nombre de un canal. Se reutilizan las mismas guardas de ``_witnesses`` para
    no mandar como artista un sello o un bloque dejado por el descargador.
    """
    return tuple(_witnesses(raw or {}, hints or {}, ""))


def _plausible_artist(name):
    """Un artista es un nombre corto, no el bloque de texto de un descargador.

    Sin esto, "Heavenly (SYNTHWAVE, CHILL, EDM, CLUB) HIT 2025 | No copyright /
    Royalty-Free" pasaba por artista solo porque el titulo se podia partir.
    """
    limpio = collapse(name)
    if not limpio or len(limpio) > 60 or len(limpio.split()) > 7:
        return False
    return "|" not in limpio


def _looks_swapped(artist_raw, title_raw, witness):
    """Estan cambiados los campos: el titulo en artista y el artista en titulo?

    Lo decide un testigo externo. Si el albumartist o la carpeta se parecen al
    campo TITULO y no al campo ARTISTA, los dos estan al reves. Es el caso de
    "ME VOY MUY LEJOS - HERNAN Y LA CHAMPIONS LIGA.mp3", con el nombre de la
    cancion metido en el campo de artista, y el de "Oft Gefragt -
    AnnenMayKantereit.mp3", donde el albumartist delataba al grupo.
    """
    if not witness or not artist_raw or not title_raw:
        return False
    # El campo de titulo va a pasar a ser el ARTISTA, y el de artista a ser el
    # TITULO. Si lo que hay en artista es un canal o un sello, el intercambio
    # convierte "Warner Records Vault" en el nombre de la cancion.
    from .filenames import looks_like_channel

    if looks_like_channel(artist_raw):
        return False
    # El campo de titulo va a pasar a ser el artista, y un artista es un nombre,
    # no un bloque "Grupo - Cancion". Sin esto, "Kumbia All-Starz - Invisible"
    # acababa de artista.
    if SPLIT_RE.search(title_raw) or not _plausible_artist(title_raw):
        return False
    # Con jaccard, no con overlap: el testigo tiene que ser practicamente TODO
    # el campo de titulo. "solo quiero amarte la champions liga" contiene el
    # nombre del grupo, pero no es el nombre del grupo, y ahi no hay nada
    # cambiado de sitio.
    parecido_artista = overlap(artist_raw, witness)
    parecido_titulo = jaccard(title_raw, witness)
    return (parecido_titulo >= 0.6 and parecido_titulo > parecido_artista
            and parecido_artista < 0.4)


def _unsupported_artist(artist_raw, witnesses):
    """Ningun testigo respalda a este artista: ni el archivo, ni la carpeta.

    Por si solo no basta para tirarlo: "Tëmnaja noč.mp3" de Марк Бернес tampoco
    lo respalda nadie y el tag es correcto. Solo abre la puerta a sustituirlo
    por un candidato que SI este confirmado.
    """
    if not artist_raw:
        return False
    return all(overlap(testigo, artist_raw) <= 0 for testigo in witnesses)


def _is_known(name, known):
    """Este nombre lo usa la biblioteca como artista en otros temas?"""
    if not name or not known:
        return False
    return norm(main_artist(clean_artist(name))) in known


def _artist_side(left, right, hints, raw, known):
    """De que lado de "A - B" esta el artista. Devuelve "izq", "der" o "".

    Se pregunta, en este orden, a la carpeta del artista, al albumartist y a los
    nombres que la biblioteca ya usa como artista. "Me Enamoré-Kumbia Kings" se
    reparte bien porque Kumbia Kings sale en otras canciones y "Me Enamoré" no.
    """
    testigos = [hints.get("artist_folder", "")]
    bruto = raw.get("albumartist", "")
    if bruto and not is_placeholder("artist", bruto):
        testigos.append(clean_artist(bruto))
    for testigo in testigos:
        if not testigo:
            continue
        izq, der = overlap(left, testigo), overlap(right, testigo)
        if der >= 0.6 and der > izq:
            return "der"
        if izq >= 0.6 and izq > der:
            return "izq"

    izq_conocida, der_conocida = _is_known(left, known), _is_known(right, known)
    if der_conocida and not izq_conocida:
        return "der"
    if izq_conocida and not der_conocida:
        return "izq"
    return ""


def _rescue_artist(artist_raw, title_raw, stem, hints, raw, known=None):
    """Candidato a artista confirmado por algo que no sea el tag.

    Devuelve (artista, titulo, confianza) o None. Dos formas de rescate:

      1. el titulo trae "Artista - Titulo" pegado y aparece en el nombre del
         archivo ("moebus100" + "Bob Marley-Sweat ..."). De que lado esta el
         artista lo decide _artist_side; si no lo sabe nadie se asume el orden
         dominante y baja la confianza, porque "Titulo - Artista" tambien pasa.
      2. el archivo esta en la carpeta del artista y el nombre del archivo lo
         nombra ("la9enelpecho" + "solo quiero amarte la champions liga")
      3. el titulo trae "Algo - Titulo" y el archivo "Artista - Titulo" con el
         mismo titulo. "감자농사" + "[가사번역] 새로운 학교의 리더즈 - Oi AG!" en
         "ATARASHII GAKKO! - Oi AG!.mp3": el tag es el video de un canal que
         traduce letras, con el grupo escrito en otro alfabeto. El titulo
         compartido confirma que el archivo habla de la misma cancion, y de
         quien es lo dice el archivo.
    """
    partes = SPLIT_RE.split(title_raw, maxsplit=1)
    if len(partes) != 2:
        partes = TIGHT_SPLIT_RE.split(title_raw, maxsplit=1)
    if len(partes) == 2:
        izquierda, derecha = partes[0].strip(), partes[1].strip()
        if (len(izquierda) >= 2 and len(derecha) >= 2
                and overlap(stem, izquierda) >= 0.6
                and overlap(izquierda, artist_raw) < 0.4):
            lado = _artist_side(izquierda, derecha, hints, raw, known)
            if lado == "der" and _plausible_artist(derecha):
                return derecha, izquierda, "alta"
            if lado != "der" and _plausible_artist(izquierda):
                return izquierda, derecha, "alta" if lado == "izq" else "media"

        from .filenames import SPLIT_RE as FILE_SPLIT_RE, looks_like_channel

        trozos = [x.strip() for x in FILE_SPLIT_RE.split(stem) if x.strip()]
        if len(trozos) == 2:
            del_archivo, su_titulo = clean_pair(trozos[0], trozos[1])
            clave = norm(bare_title(su_titulo))
            if (clave and clave == norm(bare_title(strip_noise(derecha)))
                    and _plausible_artist(del_archivo)
                    and not looks_like_channel(del_archivo)):
                return del_archivo, su_titulo, "media"

    carpeta = hints.get("artist_folder", "")
    if carpeta and overlap(stem, carpeta) >= 0.6:
        titulo = strip_artist_affix(strip_noise(title_raw), carpeta)
        if titulo and overlap(titulo, carpeta) < 0.6:
            return carpeta, titulo, "alta"
    return None


def _album_artist_fiable(raw, artist):
    """El albumartist que ya hay, salvo que sea copia de un artista descartado.

    Los descargadores escriben el canal en los dos campos. Si el de artista se
    ha desechado por no ser el artista, su copia en albumartist tampoco lo es:
    conservarla dejaba "ATARASHII GAKKO!" agrupado bajo "감자농사".
    """
    bruto = raw.get("albumartist", "")
    if is_placeholder("artist", bruto):
        return ""
    actual = collapse(bruto)
    del_tag = collapse(raw.get("artist", ""))
    if (artist and del_tag and actual.casefold() == del_tag.casefold()
            and overlap(del_tag, artist) <= 0
            and not same_text(del_tag, artist)):
        return ""
    return actual


def _swap_empeora(artist_raw, title_raw, known):
    """Intercambiar estropearia unos tags que ya estaban bien?

    Que el nombre del archivo diga "Titulo - Artista" no prueba que los tags
    esten cambiados: prueba que el nombre esta escrito al reves. Lo decide el
    vocabulario de la biblioteca, que es evidencia de fuera de este archivo.

    Solo frena el intercambio cuando hay una respuesta clara: el artista que ya
    hay se usa como artista en otros temas y el candidato no. Si no sabe de
    ninguno de los dos, se deja pasar la regla de siempre.
    """
    if not known:
        return False
    actual_conocido = _is_known(artist_raw, known)
    candidato_conocido = _is_known(title_raw, known)
    return actual_conocido and not candidato_conocido


def resolve_identity(path, raw, hints, artist_suspect=False, known=None):
    """Artista y titulo definitivos, en orden de fiabilidad.

    tags limpios -> nombre de archivo -> carpeta del artista.
    Devuelve (artista, titulo, fuente, confianza).

    Si el artista del tag es un canal de YouTube (artist_suspect), no sirve para
    nada: ni como artista, ni para separar el "Artista - Titulo" del titulo. En
    ese caso manda el nombre del archivo. Sin esto, "Apache 207 - Miami.mp3" con
    el tag "Awesome Music" se quedaba con el titulo "Apache 207 - Miami", porque
    el prefijo no se parecia al tag y no habia con que confirmarlo.
    """
    corrected = identity_correction(path)
    if corrected:
        artist, title = clean_pair(*corrected)
        return artist, title, "correccion manual", "alta"

    artist_raw = "" if is_placeholder("artist", raw.get("artist", "")) else raw.get("artist", "")
    title_raw = "" if is_placeholder("title", raw.get("title", "")) else raw.get("title", "")

    # "Luciano | Locosquad" trae el canal pegado con una barra. Se resuelve
    # contra el nombre del archivo, que es quien sabe cual de los dos es.
    from .filenames import SPLIT_RE as FILE_SPLIT_RE, clean_stem
    stem = clean_stem(path)
    artist_raw = split_pipe_artist(artist_raw, stem)

    # Campos intercambiados: el tag de artista se parece al lado DERECHO del
    # nombre del archivo y el de titulo al IZQUIERDO. Pasa cuando el descargador
    # metio el titulo en el campo de artista, como en
    # "FEUERSCHWANZ - Dragostea Din Tei_ Napalm Records".
    partes = [x.strip() for x in FILE_SPLIT_RE.split(stem) if x.strip()]
    if len(partes) >= 2 and artist_raw and title_raw:
        izquierda, derecha = partes[0], " - ".join(partes[1:])
        # El lado derecho tiene que SER el artista, no solo contenerlo. Con
        # overlap bastaba con que sus palabras aparecieran ahi dentro, y un
        # lado derecho largo contiene casi cualquier cosa: en
        # "Kinderlied Kuken / Wo ist meine Mama / TiRiLi - Kinderlieder" daba
        # 1,0 y mandaba a intercambiar unos tags que ya estaban bien.
        # Ojo: esto es ambiguo por naturaleza. "16 Shots - Stefflon Don.mp3"
        # con artista "Stefflon Don" y titulo "16 Shots" esta BIEN, y desde el
        # nombre se ve igual que un archivo con los campos cambiados: en los dos
        # el tag de artista se parece al lado derecho. Asi que no basta con el
        # nombre; hace falta que la biblioteca confirme que el candidato a
        # artista lo es de verdad. Si no lo sabe, se dejan los tags como estan y
        # ya lo delatara la sonda de campos cambiados de las fuentes, que si
        # tiene con que decidirlo.
        if (jaccard(derecha, artist_raw) >= 0.6
                and overlap(izquierda, title_raw) >= 0.6
                and overlap(izquierda, artist_raw) < 0.4
                and _is_known(title_raw, known)
                and not _swap_empeora(artist_raw, title_raw, known)):
            artist_raw, title_raw = title_raw, artist_raw

    # Campos cambiados que el nombre del archivo no puede delatar porque el
    # archivo esta en el mismo orden equivocado. Aqui deciden los testigos de
    # fuera: el albumartist y la carpeta del artista.
    testigos = _witnesses(raw, hints, stem)
    fuente_extra = ""
    if artist_raw and title_raw:
        for testigo in testigos:
            if testigo is stem:
                continue
            if (_looks_swapped(artist_raw, title_raw, testigo)
                    and not _swap_empeora(artist_raw, title_raw, known)):
                artist_raw, title_raw = title_raw, artist_raw
                artist_suspect = False
                fuente_extra = "campos cambiados"
                break

    # Un artista al que no respalda nada: ni el archivo, ni la carpeta, ni el
    # albumartist. Solo se sustituye si hay un candidato confirmado.
    confianza_extra = "alta"
    if not fuente_extra and title_raw and _unsupported_artist(artist_raw, testigos):
        rescate = _rescue_artist(artist_raw, title_raw, stem, hints, raw, known)
        if rescate:
            artist_raw, title_raw, confianza_extra = rescate
            artist_suspect = False
            fuente_extra = "artista sin respaldo"

    if fuente_extra:
        artist, title = clean_pair(artist_raw, title_raw)
        if artist and title:
            return artist, title, fuente_extra, confianza_extra

    if artist_suspect:
        # El nombre del archivo sirve para saber QUIEN es el artista. El titulo
        # se sigue sacando del tag, que suele estar mas completo: el archivo
        # "Frenzy - Lampen.mp3" venia truncado y el tag decia "Lampen an".
        file_artist, file_title, _ = parse_filename(
            path, artist_hint=hints["artist_folder"])
        if file_artist:
            from_tag, _ = strip_artist_prefix(strip_noise(title_raw), file_artist)
            from_tag = collapse(from_tag)
            # El titulo del tag solo vale si ya NO lleva el artista dentro. Si lo
            # lleva es que el reparto fallo, y entonces manda el nombre del
            # archivo: "AYLIVA X APATSCHE 207 | WUNDER" se queda en "WUNDER".
            usa_tag = (from_tag
                       and overlap(from_tag, file_artist) < 0.5
                       and len(from_tag) > len(file_title or ""))
            best = from_tag if usa_tag else (file_title or from_tag)
            if best:
                return file_artist, best, "archivo (tag no fiable)", "media"
        artist_raw = ""

    artist, title = clean_pair(artist_raw, title_raw)
    source, confidence = "limpieza", "alta"

    if not artist or not title:
        hint = artist or hints["artist_folder"]
        f_artist, f_title, f_conf = parse_filename(path, artist_hint=hint)
        if not artist and f_artist:
            artist, source, confidence = f_artist, "archivo", f_conf
        if not title and f_title:
            title, source, confidence = f_title, "archivo", f_conf

    # Sin artista y sin separador, el album dice donde acaba el nombre del
    # grupo: "Джаро Ханза Королева танцпола.mp3" no tiene ni un guion, pero su
    # album es "Королева танцпола", que es exactamente el titulo. Lo que sobra
    # por delante es el artista.
    if not artist and title:
        bruto = raw.get("album", "")
        album_raw = "" if is_placeholder("album", bruto) else strip_noise(bruto)
        objetivo = norm(album_raw)
        palabras = title.split()
        if objetivo and len(palabras) > 1 and objetivo != norm(title):
            for tam in range(len(palabras) - 1, 0, -1):
                if norm(" ".join(palabras[-tam:])) != objetivo:
                    continue
                posible = clean_artist(collapse(" ".join(palabras[:-tam])))
                if len(posible) >= 3:
                    artist, title = posible, collapse(" ".join(palabras[-tam:]))
                    source, confidence = "el album era el titulo", "media"
                break

    if not artist and hints["artist_folder"]:
        artist = clean_artist(hints["artist_folder"])
        source, confidence = "carpeta", "media"

    # Un titulo identico al artista es basura: pasa cuando el tag trae
    # "LUCIANO feat. NIMO - Valentino Camouflage" y el reparto se queda con el
    # lado equivocado. Manda el nombre del archivo.
    if title and artist and norm(title) == norm(main_artist(artist)):
        from .filenames import parse_filename as _parse
        f_artist, f_title, _c = _parse(path, artist_hint=artist)
        if f_title and norm(f_title) != norm(title):
            title = f_title
            source, confidence = "archivo (titulo = artista)", "media"

    # Un titulo que es solo un parentesis no es un titulo: "(letra y acordes)".
    # El de verdad se fue a otro campo, y el nombre del archivo suele tenerlo.
    if title:
        from .text import only_brackets

        if only_brackets(title):
            from .filenames import parse_filename as _parse_par

            f_artist, f_title, _c = _parse_par(path, artist_hint=hints.get(
                "artist_folder", ""))
            if f_title and not only_brackets(f_title):
                title = f_title
                if f_artist:
                    artist = f_artist
                source, confidence = "archivo (el titulo era un parentesis)", "media"

    # Un titulo que nombra un canal o un sello no es un titulo. Pasa cuando el
    # descargador dejo el canal en un campo y algo lo ascendio: "Earshot - Wait
    # _ Warner Vault.mp3" acabo llamandose "Warner Records Vault". Manda el
    # nombre del archivo, que ahi si dice "Wait".
    if title:
        from .filenames import looks_like_channel

        if looks_like_channel(title):
            from .filenames import parse_filename as _parse_canal

            f_artist, f_title, _c = _parse_canal(path, artist_hint=artist)
            if f_title and not looks_like_channel(f_title):
                title = f_title
                if not artist and f_artist:
                    artist = f_artist
                source, confidence = "archivo (el titulo era un canal)", "media"

    # Y el canal tambien puede quedarse colgando al final del titulo, con un
    # nombre que ninguna lista de palabras reconoce ("Warner Vault"). Lo delata
    # el propio archivo: es lo que traia el tag de artista o el de albumartist.
    if title:
        from .text import strip_channel_tail

        for candidato in (raw.get("artist", ""), raw.get("albumartist", "")):
            if not candidato or is_placeholder("artist", candidato):
                continue
            recortado = strip_channel_tail(title, candidato)
            if recortado and recortado != title:
                title = recortado
                if source == "limpieza":
                    source = "limpieza (canal al final del titulo)"
                break

    # Sin titulo de ninguna clase, el nombre del archivo limpio es mejor que
    # dejarlo vacio, aunque no tenga separador y no se sepa donde acaba el
    # artista. "TRYNA (feat. Samra).mp3" se quedaba sin nada.
    if not title:
        from .filenames import clean_stem
        desnudo = strip_noise(clean_stem(path))
        if desnudo:
            title = desnudo
            source, confidence = "nombre del archivo", "media"

    return artist, title, source, confidence


def identity_for(path, root, suspect_artists=None, known=None):
    """Version autonoma de resolve_identity para quien solo tiene la ruta."""
    from .tags import read_tags

    raw = read_tags(path)
    if raw is None:
        return "", "", raw
    artist, title, _, _ = resolve_identity(
        path, raw, folder_hints(path, root),
        artist_suspect=str(path) in (suspect_artists or ()), known=known)
    return artist, title, raw


# Palabras que un nombre de archivo lleva ademas del artista y el titulo sin
# decir nada de la cancion. Ni "remix" ni "live": esas si cambian de grabacion.
_RELLENO = {"feat", "ft", "featuring", "x", "y", "e", "and", "vs", "con", "de",
            "del", "la", "el", "los", "las", "the", "a", "prod"}

# Palabras que dicen QUE grabacion es. Si el archivo nombra una y la fuente no,
# no estan hablando de lo mismo.
_VERSIONES = {"remix", "mix", "edit", "live", "vivo", "directo", "acoustic",
              "acustico", "instrumental", "extended", "version", "karaoke",
              "cover", "remastered", "remasterizado", "unplugged"}


def _una_letra(a, b):
    """La misma palabra, o con una letra de mas o de menos si es larga."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    corta, larga = sorted((a, b), key=len)
    i = 0
    while i < len(corta) and corta[i] == larga[i]:
        i += 1
    if len(corta) == len(larga):
        return corta[i + 1:] == larga[i + 1:]
    return corta[i:] == larga[i + 1:]


def _contenidas(palabras, en):
    """Todas las palabras de un lado estan en el otro, admitiendo erratas."""
    return bool(palabras) and all(any(_una_letra(p, q) for q in en)
                                  for p in palabras)


def _desempate(match, artista, titulo, raw, hints, path, duracion, sospechoso):
    """Una fuente elige entre lo que ya habia, sin inventar nada.

    Devuelve (artista, titulo, confianza) o None. Tienen que darse las cuatro:

      1. la duracion de la fuente cuadra con la del archivo;
      2. su artista esta escrito en el nombre del archivo, la carpeta, el
         albumartist o el campo de titulo (cuando estaban cambiados);
      3. su titulo es exactamente uno de los que ya habia, o el nombre del
         archivo es literalmente su artista mas su titulo;
      4. al artista que hay no lo respalda nada, o es un canal, o es el titulo.

    Es la politica de una sola fuente, coincidencia exacta y artista
    verificado, solo que el artista lo verifica el propio archivo. Una fuente da
    "media"; dos de acuerdo, "alta". Lo que la fuente dice y no aparece en
    ningun sitio ("Celia Cruz" para "Quimbara.mp3") no pasa: eso seria inventar.
    """
    nuevo_artista = collapse(match.get("artist", ""))
    nuevo_titulo = collapse(match.get("title", ""))
    propia = float(match.get("duration") or 0)
    if not (nuevo_artista and nuevo_titulo and duracion and propia):
        return None
    if abs(propia - duracion) > duration_tolerance(duracion):
        return None

    nombre = strip_noise(clean_stem(path))
    en_nombre = tokens(nombre)
    albumartist = raw.get("albumartist", "")
    if is_placeholder("artist", albumartist):
        albumartist = ""
    apoyos = [tokens(x) for x in (nombre, hints.get("artist_folder", ""),
                                  albumartist) if x]
    testigos = apoyos + [tokens(raw.get("title", ""))]

    nombres = artist_names(nuevo_artista) or [nuevo_artista]
    if not any(_contenidas(tokens(n), t) for n in nombres for t in testigos):
        return None

    clave = norm(bare_title(nuevo_titulo))
    f_artista, f_titulo, _confianza = parse_filename(path)
    locales = (titulo, artista, raw.get("title", ""), raw.get("artist", ""),
               f_artista, f_titulo)
    exacto = bool(clave) and any(norm(bare_title(x)) == clave
                                 for x in locales if x)
    if not exacto:
        permitidas = (set(_RELLENO) | tokens(nuevo_titulo)
                      | tokens(match.get("album", "")))
        for n in nombres:
            permitidas |= tokens(n)
        exacto = (_contenidas(tokens(bare_title(nuevo_titulo)), en_nombre)
                  and _contenidas(en_nombre, permitidas))
    if not exacto:
        return None

    # El archivo dice que es una version concreta y la fuente habla de otra
    # cosa: "SURTADA REMIX" no es "Surtada". Aqui no se desempata nada.
    if (en_nombre & _VERSIONES) - tokens(nuevo_titulo):
        return None

    mismo = bool(artista) and (same_text(artista, nuevo_artista)
                               or norm(artista) == norm(nuevo_artista))
    cambiados = bool(artista) and norm(bare_title(artista)) == clave
    if artista and not sospechoso and not mismo and not cambiados:
        respaldado = any(_contenidas(tokens(artista), t) for t in apoyos)
        if respaldado:
            return None

    confianza = "alta" if match.get("agree", 0) >= 2 else "media"
    if cambiados:
        # Estaban al reves: se les da la vuelta a los dos campos que YA habia.
        # No se copia el texto de la fuente, asi no se pierde un "(Remix)" ni
        # se cuela un "(Ao Vivo)" que el archivo no tenia.
        return titulo or nuevo_artista, artista, confianza
    if mismo or not artista:
        # La fuente confirma lo que ya se habia decidido: no cambia nada, solo
        # sube la confianza.
        return None, None, confianza
    # A un artista sin respaldo lo sustituye el de la fuente, pero el titulo se
    # queda como estaba: de el no hay nada que dudar.
    return nuevo_artista, titulo or nuevo_titulo, confianza


def analyze(path, root, mb_client=None, suspect_artists=None, verify=False,
            artist_genres=None, known=None, artist_spellings=None,
            analysis_meta=None, search_variants=None, album_spellings=None):
    """Devuelve (propuestas, nota) para un archivo ya leido."""
    from .tags import read_tags

    if analysis_meta is not None:
        analysis_meta.clear()
        analysis_meta.update({
            "online_attempted": False,
            "online_match": False,
            "online_confidence": "",
            "online_source": "",
        })

    raw = read_tags(path)
    if raw is None:
        return [], "ilegible"

    hints = folder_hints(path, root)
    artist_suspect = str(path) in (suspect_artists or ())
    artist, title, source, confidence = resolve_identity(
        path, raw, hints, artist_suspect=artist_suspect, known=known)
    artist_local, title_local = artist, title
    # Lo que decidio una persona en correcciones.json manda sobre todo lo
    # demas, fuentes incluidas.
    fijos = {campo: collapse(valor)
             for campo, valor in correction_for(path).items()
             if campo in CAMPOS_DE_TAG}

    note = ""
    if is_unrecoverable(path) and not artist:
        note = "sin datos recuperables (necesita huella acustica)"

    # albumartist agrupa en los reproductores. Se deja el que ya haya; si no
    # hay, se pone el artista principal sin invitados. Nunca se rellena a partir
    # de un artista que el comando "artistas" ha marcado como sospechoso: seria
    # propagar el nombre de un canal de YouTube a un campo mas.
    album_artist = _album_artist_fiable(raw, artist)
    # Una correccion manual confirma tambien quien debe agrupar el album. Si se
    # conserva aqui el canal/editor original (p. ej. Sandfall Interactive), el
    # reproductor sigue mostrando la cancion bajo el artista equivocado.
    if identity_correction(path) and artist:
        album_artist = main_artist(artist)
    elif not album_artist and artist:
        album_artist = main_artist(artist)

    album = _clean_album(raw["album"], artist, title)
    genre = "" if is_placeholder("genre", raw["genre"]) else collapse(raw["genre"])
    date = clean_date(raw["date"])
    track = clean_track_number(raw["tracknumber"])

    genre_source = "limpieza"
    folder_genre = genre_for_folder(hints["genre_folder"])
    genre_clash = ""
    if not genre:
        if folder_genre:
            genre, genre_source = folder_genre, "carpeta"
    elif folder_genre:
        from .genre_check import check as _genre_check
        if _genre_check(path, root, genre) is not None:
            genre_clash = folder_genre

    duration = None
    mb_match = None
    # Si la correccion ya lo dice todo, no hay nada que preguntar a nadie.
    todo_decidido = all(fijos.get(c) for c in
                        ("artist", "title", "album", "date", "genre"))
    if mb_client is not None and title and not note and not todo_decidido:
        # Por defecto solo se consulta para rellenar huecos. Con verify tambien
        # se comprueba lo que ya esta puesto: hay albumes visiblemente mal, como
        # "Spaceman (Original Mix)" archivado bajo "Spaceman (Carnage Festival
        # Trap Remix)", que de otro modo no se tocan nunca.
        needs_online = verify or not album or not date
        if needs_online:
            if analysis_meta is not None:
                analysis_meta["online_attempted"] = True
            from .tags import read_basic_audio_info
            info = read_basic_audio_info(path)
            duration = (info or {}).get("duration") or None
            buscar = getattr(mb_client, "consensus_lookup", None) \
                if getattr(mb_client, "consensus_mode", False) else None
            query_args = {
                "duration": duration,
                "hints": online_artist_hints(raw, hints),
            }
            if search_variants is not None:
                query_args["max_variants"] = search_variants
            mb_match = (buscar or mb_client.lookup)(artist, title, **query_args)
            if analysis_meta is not None and mb_match:
                analysis_meta.update({
                    "online_match": True,
                    "online_confidence": mb_match.get(
                        "confidence", "revisar"),
                    "online_source": mb_match.get("source", "online"),
                })

    if mb_match and mb_match.get("swapped"):
        # Las fuentes solo conocian el tema con el artista y el titulo al
        # reves. Cuando el nombre del archivo esta equivocado igual que el tag
        # ("Welcome to Jamrock - Damian Marleys.mp3") esta es la unica prueba
        # que existe, porque no queda ninguna pista local que contrastar.
        nuevo_artista = collapse(mb_match.get("artist") or title)
        nuevo_titulo = collapse(mb_match.get("title") or artist)
        if nuevo_artista and nuevo_titulo:
            artist, title = nuevo_artista, nuevo_titulo
            source = "%s (campos cambiados)" % (mb_match.get("source") or "online")
            confidence = "alta" if mb_match.get("agree", 0) >= 2 else "revisar"
            album_artist = main_artist(artist)

    # La coincidencia online tambien puede reparar el credito. Antes solo se
    # aprovechaban album/fecha/genero: un tema encontrado sin artista seguia
    # anonimo (o conservaba "brasil"), y "David Guetta Bebe Rexha" nunca ganaba
    # el separador que las fuentes si conocen.
    if mb_match:
        nuevo_artista = collapse(mb_match.get("artist", ""))
        nuevo_titulo = collapse(mb_match.get("title", ""))
        mb_confianza = mb_match.get("confidence", "revisar")
        testigos_online = online_artist_hints(raw, hints)
        izquierda, _ = split_title_pair(title)
        mismo_credito = (artist and nuevo_artista
                          and norm(artist) == norm(nuevo_artista)
                          and artist != nuevo_artista)
        credito_mas_completo = bool(
            artist and nuevo_artista and tokens(artist) < tokens(nuevo_artista))
        testigo_nuevo = any(
            (overlap(testigo, nuevo_artista) >= 0.6
             or same_text(testigo, nuevo_artista))
            and overlap(testigo, artist) < 0.6
            for testigo in testigos_online)
        artista_en_titulo = bool(
            izquierda and nuevo_artista
            and (overlap(izquierda, nuevo_artista) >= 0.6
                 or same_text(izquierda, nuevo_artista))
            and overlap(izquierda, artist) < 0.6)

        usar_identidad_online = (
            not artist or not _plausible_artist(artist)
            or (mb_confianza in WRITABLE and (
                mismo_credito or credito_mas_completo
                or testigo_nuevo or artista_en_titulo)))
        if usar_identidad_online and nuevo_artista and nuevo_titulo:
            # Las fuentes a veces devuelven la pista base aunque el archivo sea
            # un remix/edit concreto. Se corrige el artista, pero se conserva la
            # version local si el resultado online perdio ese calificador.
            titulo_base = nuevo_titulo
            if (VERSION_MARKER_RE.search(title)
                    and not VERSION_MARKER_RE.search(nuevo_titulo)):
                titulo_base = title
            artist, title = clean_pair(nuevo_artista, titulo_base)
            source = mb_match.get("source") or "online"
            confidence = mb_confianza
            album_artist = main_artist(artist)

    # Desempate: la fuente elige entre lo que ya habia, sin inventar nada. Si lo
    # verifica el propio archivo, su album y su fecha valen como los de una
    # coincidencia exacta.
    if mb_match and not fijos:
        elegido = _desempate(mb_match, artist_local, title_local, raw, hints,
                             path, duration, artist_suspect)
        if elegido:
            nuevo_artista, nuevo_titulo, conf = elegido
            if mb_match.get("confidence") not in WRITABLE:
                mb_match = dict(mb_match, confidence=conf)
            cambia = bool(nuevo_artista and nuevo_titulo) and (
                (norm(nuevo_artista), norm(nuevo_titulo))
                != (norm(artist), norm(title)))
            if cambia:
                artist, title = nuevo_artista, nuevo_titulo
                album_artist = main_artist(artist)
            if cambia or confidence not in WRITABLE:
                source = "%s (desempate)" % (mb_match.get("source") or "online")
                confidence = conf

    # No se deja que una fuente o un titulo conviertan una marca deliberada en
    # Title Case. La grafia solo se toma de pistas locales que ya contienen el
    # mismo nombre, por lo que BABYMETAL se conserva sin proteger un autor
    # distinto o un canal de descarga.
    referencias_canonicas = tuple(
        (artist_spellings or {}).get(norm(name), "")
        for name in artist_names(artist))
    referencias_de_grafia = referencias_canonicas + (
        raw.get("artist", ""), raw.get("albumartist", ""),
        hints.get("artist_folder", ""),
    )
    artist = preserve_artist_case(artist, referencias_de_grafia)
    album_artist = preserve_artist_case(album_artist, referencias_de_grafia)

    if mb_match:
        mb_conf = mb_match["confidence"]
        origin = mb_match.get("source") or "online"
        album_mismatch = ""
        album_mismatch_conf = "revisar"
        if not album and mb_match["album"]:
            album = mb_match["album"]
            album_source, album_conf = origin, mb_conf
        else:
            album_source, album_conf = "limpieza", "alta"
            # El album que ya hay no coincide con el que dicen las fuentes. Se
            # cambia solo con acuerdo o con una coincidencia individual fuerte;
            # una respuesta apenas parecida sigue marcada para revisar.
            if verify and album and mb_match["album"]:
                from .providers import album_key, compilation_penalty
                if album_key(album) != album_key(mb_match["album"]):
                    # Solo se propone el cambio si el album nuevo no huele mas a
                    # recopilatorio que el que ya hay. Cambiar
                    # "Apollo (The Remixes)" por "The Story of Hardwell (Best
                    # Of)" no es corregir nada.
                    if (compilation_penalty(mb_match["album"])
                            <= compilation_penalty(album)):
                        album_mismatch = mb_match["album"]
                        if mb_conf in WRITABLE:
                            album_mismatch_conf = mb_conf
        if not date and mb_match["date"]:
            date = mb_match["date"]
            date_source, date_conf = origin, mb_conf
        else:
            date_source, date_conf = "limpieza", "alta"
            # La que ya hay puede ser la fecha en que se descargo el video. Se
            # corrige solo hacia atras: el ano mas antiguo es el de la edicion
            # original, y el posterior es el que puso el descargador. Al reves
            # no, porque una fecha anterior a la de la fuente puede ser el disco
            # original frente a una reedicion.
            if verify and date and mb_match["date"] and mb_conf in WRITABLE:
                nuestro, suyo = date[:4], mb_match["date"][:4]
                if nuestro.isdigit() and suyo.isdigit() and nuestro > suyo:
                    date = mb_match["date"]
                    date_source, date_conf = "%s (era posterior)" % origin, mb_conf
        # Carpetas que son idioma o cajon de sastre (Aleman, Ruso, Frances,
        # Ramdom) no dan genero. Aqui es donde se puede rellenar.
        if not genre:
            online_genre = genre_from_online(mb_match.get("genres"))
            if online_genre:
                genre, genre_source = online_genre, origin
    else:
        album_mismatch = ""
        album_mismatch_conf = "revisar"
        album_source, album_conf = "limpieza", "alta"
        date_source, date_conf = "limpieza", "alta"

    # La biblioteca manda en como se escribe un disco que ya tiene. Solo cambia
    # mayusculas y acentos: la forma normalizada es la misma, asi que no puede
    # mandar una cancion a otro album.
    if album and album_spellings:
        canonico = album_spellings.get(norm(album))
        if canonico and canonico != album:
            album = canonico

    candidates = {
        "artist": (artist, source, confidence),
        "title": (title, source, confidence),
        "album": (album, album_source, album_conf),
        "albumartist": (album_artist, "limpieza", confidence),
        "genre": (genre, genre_source, "alta"),
        "date": (date, date_source, date_conf),
        "tracknumber": (track, "limpieza", "alta"),
    }
    if fijos:
        if fijos.get("artist") and not fijos.get("albumartist"):
            fijos["albumartist"] = main_artist(fijos["artist"])
        for campo, valor in fijos.items():
            candidates[campo] = (valor, "correccion manual", "alta")

    proposals = []
    for field, (value, field_source, field_conf) in candidates.items():
        current = raw.get(field, "")
        if value == current:
            continue
        if not value and not current:
            continue
        proposals.append(Proposal(field, current, value, field_source, field_conf))

    # El titulo conserva forma "A - B" y "A" no se parece al artista: casi
    # siempre es el nombre del video con el artista pegado delante, pero a veces
    # "A" es parte real del titulo. Se propone aparte y marcado para revisar,
    # asi nunca se escribe sin que el usuario lo pida.
    # El titulo que tenemos no es el que dice la fuente, y no es una variante
    # ("(Single Version)", "[Explicit]") sino otro titulo. No se cambia solo
    # -el titulo nunca se toma de una fuente- pero tiene que verse en el
    # informe en vez de pasar callado.
    if verify and mb_match and title and mb_match.get("title"):
        from .text import title_matches as _titulos_cuadran

        suyo = collapse(mb_match["title"])
        if suyo and norm(suyo) != norm(title) and not _titulos_cuadran(suyo, title):
            proposals.append(Proposal("title", raw.get("title", ""), suyo,
                                      "no cuadra con las fuentes", "revisar"))

    if album_mismatch:
        proposals.append(Proposal("album", raw.get("album", ""), album_mismatch,
                                  "no cuadra con las fuentes",
                                  album_mismatch_conf))

    # El genero del tag contradice a la carpeta donde lo pusiste. No se decide
    # solo: puede fallar el tag o puede estar el archivo mal colocado.
    # Ultimo recurso para el genero: lo que ya tienen otras canciones del mismo
    # artista. Se aplica despues de las fuentes online, no antes.
    if not genre and artist_genres:
        heredado = artist_genres.get(norm(main_artist(artist)))
        if not heredado:
            heredado = artist_genres.get(norm(artist))
        if heredado:
            proposals.append(Proposal("genre", raw.get("genre", ""), heredado,
                                      "otro tema del artista", "media"))

    if genre_clash:
        proposals.append(Proposal("genre", raw.get("genre", ""), genre_clash,
                                  "choca-con-carpeta", "revisar"))

    # Un genero que no se parece a ningun genero conocido suele ser otro campo
    # metido donde no va: "Confía en Mí" era el nombre del disco. No se borra
    # solo, porque tambien hay generos raros de verdad; se propone para mirar.
    if genre and folder_genre and genre != folder_genre:
        from .genre_check import canonical_genres
        if not canonical_genres(genre):
            proposals.append(Proposal("genre", raw.get("genre", ""), folder_genre,
                                      "no parece un genero", "revisar"))

    parts = SPLIT_RE.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        left, right = parts[0].strip(), parts[1].strip()
        reference = artist or hints["artist_folder"]
        if not reference or overlap(left, reference) < 0.6:
            # Sin nada con que contrastar se asume "Artista - Titulo", que es el
            # orden dominante en descargas, pero el orden inverso existe.
            if reference and overlap(right, reference) > overlap(left, reference):
                new_title, new_artist = left, right
            else:
                new_title, new_artist = right, left
            if new_title != raw.get("title", ""):
                proposals.append(Proposal("title", raw.get("title", ""), new_title,
                                          "titulo-partido", "revisar"))
            if new_artist != raw.get("artist", ""):
                proposals.append(Proposal("artist", raw.get("artist", ""), new_artist,
                                          "titulo-partido", "revisar"))

    if fijos:
        proposals = [p for p in proposals
                     if p.field not in fijos or p.source == "correccion manual"]
    return proposals, note

"""Normalizacion y limpieza de texto de metadatos."""

import re
import unicodedata

# ---------------------------------------------------------------------------
# Normalizacion para comparar
# ---------------------------------------------------------------------------

def strip_accents(value):
    decomposed = unicodedata.normalize("NFD", value or "")
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


# Plegado que conserva la longitud: permite buscar sobre el texto sin acentos y
# cortar el texto original usando las mismas posiciones.
_FOLD_MAP = str.maketrans(
    "áàâãäåÁÀÂÃÄÅ"
    "éèêëÉÈÊË"
    "íìîïÍÌÎÏ"
    "óòôõöÓÒÔÕÖ"
    "úùûüÚÙÛÜ"
    "ñÑçÇýÿÝ",
    "aaaaaaAAAAAA"
    "eeeeEEEE"
    "iiiiIIII"
    "ooooo OOOOO".replace(" ", "")
    + "uuuuUUUU"
    + "nNcCyyY",
)


def fold(value):
    return (value or "").translate(_FOLD_MAP)


def sub_folded(pattern, repl, value):
    """Aplica el patron sobre la version sin acentos y recorta el original."""
    folded = fold(value)
    out, last = [], 0
    for match in pattern.finditer(folded):
        out.append(value[last:match.start()])
        out.append(repl)
        last = match.end()
    out.append(value[last:])
    return "".join(out)


def search_folded(pattern, value):
    """Busca sin acentos y devuelve (match, texto_original_del_grupo_1)."""
    match = pattern.search(fold(value))
    if not match:
        return None, ""
    return match, value[match.start(1):match.end(1)] if match.lastindex else ""


def norm(value):
    """Forma canonica: sin acentos, minusculas, sin puntuacion."""
    text = strip_accents(value).lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


# Cirilico a latino. "Катюша" y "katyusha" son el mismo nombre en dos
# alfabetos: sin esto no se parecen en nada y el programa no puede ver que el
# artista y el titulo son la misma palabra escrita de dos maneras.
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "i", "є": "e", "ґ": "g",
}


def translit(value):
    """Pasa el cirilico a latino. El texto latino se queda como esta."""
    if not value:
        return ""
    out = []
    for char in value:
        lower = char.lower()
        if lower in _CYRILLIC:
            replacement = _CYRILLIC[lower]
            out.append(replacement.upper() if char.isupper() else replacement)
        else:
            out.append(char)
    return "".join(out)


def key(value):
    """Forma comparable que ademas cruza alfabetos."""
    return norm(translit(value))


def squash_key(value):
    """Sin espacios ni puntuacion, y con el cirilico pasado a latino."""
    return re.sub(r"[^a-z0-9]", "", key(value).lower())


def same_text(a, b, threshold=0.86):
    """Son el mismo nombre, aunque esten escritos en alfabetos distintos?

    "Элджей" romanizado da "eldzhei" y el archivo dice "eldzei": ni son iguales
    ni comparten palabras, pero es evidentemente el mismo nombre. Se compara la
    forma pegada con una medida de parecido, que si lo ve.
    """
    from difflib import SequenceMatcher

    ka, kb = squash_key(a), squash_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if min(len(ka), len(kb)) < 4:
        return False
    return SequenceMatcher(None, ka, kb).ratio() >= threshold


def tokens(value):
    return set(norm(value).split())


_HAS_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def overlap(a, b):
    """Fraccion del conjunto mas corto que aparece en el otro.

    Si no hay ni una palabra en comun y uno de los dos esta en cirilico, se
    repite la cuenta con el cirilico romanizado: "Элджей & Кравц" y
    "eldzei-kravc" son lo mismo escrito en dos alfabetos.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    direct = len(ta & tb) / min(len(ta), len(tb))
    if direct or not (_HAS_CYRILLIC.search(a or "") or _HAS_CYRILLIC.search(b or "")):
        return direct
    ka, kb = set(key(a).split()), set(key(b).split())
    if not ka or not kb:
        return 0.0
    comunes = sum(1 for x in ka if any(same_text(x, y) for y in kb))
    return comunes / min(len(ka), len(kb))


def jaccard(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Placeholders: valores no vacios que en realidad significan "no hay dato"
# ---------------------------------------------------------------------------

PLACEHOLDERS = {
    "artist": {
        "artista desconocido", "unknown artist", "unknown", "sin artista",
        "artist", "n a", "na", "none", "varios artistas",
    },
    "album": {
        "album desconocido", "unknown album", "untitled album", "sin album",
        "album", "n a", "na", "none", "unknown",
        # Descargadores que dejan el nombre del sitio como album.
        "youtube", "you tube", "soundcloud", "spotify", "audio", "video",
    },
    "genre": {
        "genero desconocido", "genero", "unknown genre", "unknown", "music",
        "musica", "other", "otros", "sin genero", "genre", "n a", "na", "none",
        "12", "255",
    },
    "title": {
        "unknown title", "titulo desconocido", "sin titulo", "untitled",
        "track", "title", "n a", "na", "none",
        "youtube", "you tube", "soundcloud", "audio", "video",
    },
}


# Un campo que es solo el nombre de una web ("X2Download.com", "SnapSave.io")
# lo dejo el conversor y no dice nada de la cancion. Con un espacio dentro ya
# no es una web: "El Cangri.com" es un disco de Daddy Yankee.
SOLO_WEB_RE = re.compile(r"^(?:www\.)?[\w-]+\.(?:com|net|org|io|app)$", re.IGNORECASE)


def is_placeholder(field, value):
    if not value or not value.strip():
        return True
    if SOLO_WEB_RE.match(value.strip()):
        return True
    return norm(value) in PLACEHOLDERS.get(field, set())


# ---------------------------------------------------------------------------
# Ruido tipico de descargas de YouTube
# ---------------------------------------------------------------------------

_NOISE = [
    r"official\s+music\s+video", r"official\s+lyrics?\s*video",
    r"official\s+visuali[sz]er", r"official\s+hd\s+video",
    r"official\s+audio", r"official\s+video", r"official\s+song",
    # Sin espacio: "(OfficialMusic)", "(OfficialVideo)".
    r"official\s*music(\s*video)?", r"official\s*audio", r"official\s*video",
    r"official",
    r"v[ií]deo\s+oficial", r"audio\s+oficial", r"versi[oó]n\s+oficial",
    r"lyrics?\s*video", r"video\s*lyrics?", r"lyrics?", r"letras?(\s+oficial)?",
    r"letras?\s+espa[nñ]ol", r"(?:en|al)\s+espa[nñ]ol", r"espa[nñ]ol", r"deutsch", r"german", r"traducida",
    r"official\s+visuals?", r"visuals?", r"audio\s+original", r"original\s+audio",
    r"music\s+video", r"video\s+clip", r"clip\s+oficial", r"videoclip\s+oficial",
    r"musikvideo", r"videoklip", r"videoclipe", r"clipe\s+oficial",
    r"pseudo\s+v[ií]deo",
    r"offizielles\s+video", r"offizielles\s+musikvideo", r"prod\.?\s*by[^)\]]*",
    r"(?:german|deutsche?)\s+version\s+of\s+(?:a\s+)?(?:russian|english|spanish|french|italian)\s+song",
    r"\+?english\s+translation",
    r"official\s+live\s+(?:video|performance|session)", r"live\s+(?:video|performance|session|version)",
    r"en\s+vivo", r"en\s+directo", r"live", r"directo",
    r"text", r"premiere", r"premiera", r"estrenos?", r"audio\s+oficial\s+original",
    r"con\s+letra", r"videoclip", r"visuali[sz]er", r"audio", r"video",
    r"full\s*hd", r"high\s+quality", r"hd", r"hq", r"4k", r"8k",
    r"1080p?", r"720p?", r"320\s*kbps",
    r"explicit(\s+version)?", r"clean(\s+version)?",
    r"\d{4}\s*remaster(ed)?", r"remaster(ed)?(\s*\d{4})?",
    r"estreno(\s*\d{4})?", r"nuevo", r"new",
    r"free\s+download", r"descarga\s+gratis",
    # Sellos discograficos pegados al final: "Napalm Records",
    # "Sumerian Records", "Magic Phonk Release".
    r"[\w.']+\s+records?", r"[\w.']+\s+recordings?", r"[\w.']+\s+release",
    r"[\w.']+\s+entertainment", r"[\w.']+\s+label",
    r"out\s+now[!_.]*", r"ya\s+disponible", r"disponible\s+ya", r"new\s+single",
    r"official\s+soundtrack",
    r"subtitulad[oa]s?(\s+al\s+espa[nñ]ol)?", r"sub\.?\s*espa[nñ]ol",
    # Frances: los canales franceses etiquetan igual que los espanoles.
    r"audio\s+officiel", r"clip\s+officiel", r"vid[eé]o\s+officiel(le)?",
    r"paroles?(\s+officielles?)?",
    # Ruso: mismo ruido, en cirilico. Sin esto "[HD + Текст]" no se reconocia
    # como parentesis de puro ruido y se quedaba dentro del titulo.
    r"текст(\s+песни)?", r"перевод", r"слова",
    r"премьера(\s+(трека|песни|клипа|альбома))?",
    r"(?:не)?официальн(?:ый|ое|ая)\s+(?:клип|видео|аудио|премьера)",
    r"клип", r"новинка", r"слушать",
    # "(неофициальное видео, помогите получить права)": el aviso de quien sube
    # un tema que aun no esta publicado.
    r"помогите\s+получить\s+права",
    # Lo que dejan los conversores y los canales de funk: "(MP3_128K)",
    # "(KondZilla)", "(kondzilla.com)", "[Oficial Audio HD]", "[Cancion Oficial]".
    r"mp3\s*\d{2,3}\s*k(?:bps)?", r"kond\s*zilla(?:\s*\.?\s*com)?",
    r"oficial\s+(?:audio|v[ií]deo)", r"canci[oó]n\s+oficial",
    r"full\s+audio", r"audio\s+only", r"official\s+audio\s+stream",
]

# Los descargadores separan las palabras con guion o guion bajo tan a menudo
# como con espacio: "subtitulado-al-espanol", "music-video". El mismo patron
# tiene que valer para los tres, o el ruido se quita a medias y deja restos como
# "...-subtitulado-al".
_NOISE_GROUP = "|".join(
    x.replace(r"\s+", r"[\s_-]+").replace(r"\s*", r"[\s_-]*") for x in _NOISE)
_NOISE_ATOM = r"(?:%s|\d{4})" % _NOISE_GROUP

# Un parentesis entero compuesto SOLO de ruido (y separadores/anios).
PAREN_NOISE_RE = re.compile(
    r"\s*[\(\[\{]\s*%s(?:\s*[,\-\u2013\u2014/&|+_]?\s*%s)*\s*[_!.\-]*\s*[\)\]\}]" % (_NOISE_ATOM, _NOISE_ATOM),
    re.IGNORECASE,
)

# Algunos descargadores guardan un titulo cortado antes del parentesis final:
# "Eh zu kurz (Offizielles Musikvideo". Es el mismo ruido que el caso cerrado.
UNCLOSED_NOISE_RE = re.compile(
    r"\s*[\(\[\{]\s*%s(?:\s*[,\-\u2013\u2014/&|+_]?\s*%s)*\s*[_!.\-]*\s*$" % (_NOISE_ATOM, _NOISE_ATOM),
    re.IGNORECASE,
)

# Ruido pegado al final sin separador ninguno: "KINDER DEINER SONNE Lyrics",
# "Alles wird gut HQ 2010". Se exige que quede algo real delante, para no
# vaciar un titulo que sea justamente una de esas palabras.
# Las combinaciones largas van primero: si "video" ganase la carrera, "Official
# Music Video" se quedaria en "Official Music".
_BARE_WORDS = [
    r"v[ií]deo\s+oficial", r"[aá]udio\s+oficial", r"visuali[sz]er(?:\s+oficial)?",
    r"letra\s*lyrics", r"lyrics\s*letra",
    r"official\s+music\s+video", r"official\s+lyrics?\s+video",
    r"official\s+music", r"official\s+audio", r"official\s+video",
    r"music\s+video", r"lyrics?\s+video", r"full\s+audio", r"full\s+video",
    r"pseudo\s+v[ií]deo",
    r"lyrics?", r"audio", r"video", r"hd", r"hq", r"4k", r"official",
    r"visuals?", r"letras?", r"videoclip", r"full\s*hd", r"320\s*kbps",
]

# Delante tiene que quedar algo real, para no vaciar un titulo que sea justo una
# de esas palabras. Un parentesis o una comilla tambien cuentan: "(beerbongs &
# bentleys)Official Audio" venia pegado sin espacio, y "'GODS' Official Audio"
# tampoco tiene una letra delante.
BARE_TRAILING_RE = re.compile(
    r"(?:(?<=[\w'\"])\s+|(?<=[\)\]])\s*)"
    r"(?:no|not|sin|con)?\s*(?:%s)(?:\s+\d{4})?\s*$"
    % "|".join(_BARE_WORDS), re.IGNORECASE)

# Numeros escritos con cifra o con letra, al final del texto.
NUMBER_TAIL_RE = re.compile(
    r"(?:\d+|\b(?:un|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|"
    r"once|doce|quince|veinte|cien|mil)\b)\s*$", re.IGNORECASE)


def _strip_bare_trailing(text):
    """Quita el ruido pegado al final, sin comerse titulos reales.

    "Alexis & Fido - 5 Letras" es el nombre de la cancion: "Letras" en plural
    detras de un numero es parte del titulo, no el aviso de que el video lleva
    la letra escrita. En singular ("Yandel 150 Letra") si es el aviso.
    """
    for _ in range(4):
        plegado = fold(text)
        match = BARE_TRAILING_RE.search(plegado)
        if not match:
            return text
        quitado = plegado[match.start():match.end()].strip().lower()
        resto = collapse(text[:match.start()])
        if not resto:
            return text
        if quitado.endswith("letras") and NUMBER_TAIL_RE.search(resto):
            return text
        text = resto
    return text


# Id de YouTube pegado al final por el descargador: "La Buena y la Mala_1S9nW8gsjoY".
# Son once caracteres tras un guion bajo. Para no comerse una palabra real de
# once letras se exige algo que una palabra no tiene: una cifra o una mayuscula
# en mitad de minusculas.
VIDEO_ID_RE = re.compile(r"_([A-Za-z0-9_-]{11})$")
# La forma que deja yt-dlp por defecto: "Amigo [tDkDiN8x3Us]". El espacio no
# entra en el corchete, asi que un "[Radio Edit]" de verdad no se toca.
VIDEO_ID_BRACKET_RE = re.compile(r"\s*\[([A-Za-z0-9_-]{11})\]\s*$")
_PISTA_DE_ID_RE = re.compile(r"\d|[a-z][A-Z]")


def strip_video_id(value):
    for patron in (VIDEO_ID_BRACKET_RE, VIDEO_ID_RE):
        encontrado = patron.search(value or "")
        if encontrado and _PISTA_DE_ID_RE.search(encontrado.group(1)):
            return value[:encontrado.start()].rstrip(" _-") or value
    return value


# La web del conversor delante de todo: "X2Download.com - Anitta - Envolver",
# "y2meta.com - Burak Yeter - Tuesday". Solo si va seguida de un separador:
# "El Cangri.com" es un disco de Daddy Yankee y no se toca.
DOWNLOAD_PREFIX_RE = re.compile(
    r"^\s*(?:www\.)?[\w-]+\.(?:com|net|org|io|app|info|biz|ru)\s*[-–—|_:]+\s*",
    re.IGNORECASE)
WWW_RE = re.compile(r"[\(\[]?\s*\bwww\.[\w-]+\.\w{2,4}\b\s*[\)\]]?", re.IGNORECASE)

# Emoji y simbolos decorativos dentro de los tags.
TAG_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200B-\u200F\uFEFF\u203B\u2605\u2606]")

# Ruido colgando al final sin parentesis: "... | Official Video". Detras de una
# cifra el guion no separa ruido: "Morenada 2010-2012" es un rango de anos, y
# quitar "-2012" lo dejaba partido.
TRAILING_NOISE_RE = re.compile(
    r"(?<!\d)\s*(?:/{1,2}|[|\-\u2013\u2014])\s*%s(?:\s*[,\-\u2013\u2014/&|+_]?\s*%s)*\s*[_!.]*\s*$" % (_NOISE_ATOM, _NOISE_ATOM),
    re.IGNORECASE,
)

# Creditos de produccion: se comen todo hasta el final de la cadena.
PROD_RE = re.compile(
    r"\s*[\(\[]?\s*\bprod(?:uced|ucido)?\.?\s*(?:by|par|por)?\s*[:.]?\s*[^\(\)\[\]]*$",
    re.IGNORECASE,
)

# Artistas invitados. Deliberadamente NO incluye "con": en espanol es demasiado
# comun dentro de titulos reales ("Algo contigo", "Con la Cumbia en la Sangre").
FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat|ft|featuring)\.?\s+([^\)\]\[\(\u2013\u2014|]+?)\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
FEAT_INLINE_RE = re.compile(
    r"\s*[\(\[]\s*(?:feat|ft|featuring)\.?\s+([^\)\]]+?)\s*[\)\]]",
    re.IGNORECASE,
)

# Sufijos de canal, no del artista.
ARTIST_SUFFIX_RES = [
    re.compile(r"\s*[-\u2013\u2014]\s*Topic\s*$", re.IGNORECASE),
    re.compile(r"\s*[-\u2013\u2014]\s*Official(\s+(Channel|Artist|Music|Audio))?\s*$", re.IGNORECASE),
    re.compile(r"\s*Official\s+(Channel|Artist\s+Channel)\s*$", re.IGNORECASE),
    re.compile(r"\s*[-\u2013\u2014]\s*Autogenerado\s+por\s+YouTube\s*$", re.IGNORECASE),
    re.compile(r"VEVO\s*$", re.IGNORECASE),
    # Canales que se llaman "<artista> + palabra generica". El nombre real es
    # lo de delante: "Zate Musik" es el canal de ZATE.
    re.compile(r"\s+(?:musik|muzik|musica|music|kanal|channel|tv|records?|"
               r"oficial|official|offiziell|beats|sounds?|media|studio)\s*$",
               re.IGNORECASE),
]

BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")

# Un titulo que, quitados los parentesis, no deja nada: "(letra y acordes)",
# "[Official Video]". No es el nombre de una cancion, es lo que el descargador
# dejo cuando el titulo de verdad se fue a otro campo.
def only_brackets(value):
    """El texto es solo parentesis y lo que hay dentro?"""
    texto = collapse(value or "")
    if not texto or not texto.startswith(("(", "[", "{")):
        return False
    return not collapse(BRACKETED_RE.sub(" ", texto))
# "A - B", pero tambien "A; B", "A: B" y "A // B", que es como titulan muchos
# canales: "Pedro Fernandez; Yo Soy El Aventurero // Letra".
# Los dos puntos NO entran: "Seleccion de Kullaguadas: En Este Gran Poder" es
# un popurri, no "Artista: Titulo", y partirlo ahi se llevaba por delante al
# interprete de verdad. El punto y coma y la doble barra si son separadores
# fiables en los titulos que dejan los canales.
SPLIT_RE = re.compile(r"\s+[-\u2013\u2014]\s+|\s*[;]\s+|\s+//\s*")
DANGLING_RE = re.compile(r"^[\s\-\u2013\u2014|,;:._]+|[\s\-\u2013\u2014|,;:._]+$")


HASHTAG_RE = re.compile(r"(?:\s*#\w+)+\s*$")


def collapse(value):
    text = re.sub(r"\s+", " ", value or "")
    text = DANGLING_RE.sub("", text)
    return re.sub(r"\(\s*\)|\[\s*\]", "", text).strip()


def strip_noise(value):
    """Quita ruido de descarga conservando informacion real de version.

    (Remix), (Live), (Radio Edit), (Acoustic) sobreviven a proposito: identifican
    grabaciones distintas.
    """
    text = HASHTAG_RE.sub(" ", TAG_EMOJI_RE.sub(" ", value or ""))
    text = WWW_RE.sub(" ", DOWNLOAD_PREFIX_RE.sub("", text))
    for _ in range(4):
        before = text
        text = sub_folded(PAREN_NOISE_RE, " ", text)
        text = sub_folded(UNCLOSED_NOISE_RE, " ", text)
        text = sub_folded(TRAILING_NOISE_RE, "", text)
        text = _strip_bare_trailing(text)
        if text == before:
            break
    return collapse(text)


def clean_artist(value):
    text = collapse(value)
    for _ in range(3):
        before = text
        for pattern in ARTIST_SUFFIX_RES:
            candidate = pattern.sub("", text).strip()
            if len(candidate) >= 2:
                text = candidate
        if text == before:
            break
    return collapse(strip_noise(text))


def extract_featured(title):
    """Devuelve (titulo_sin_feat, [invitados])."""
    text = title or ""
    guests = []
    for pattern in (FEAT_INLINE_RE, FEAT_RE):
        while True:
            match, raw_guest = search_folded(pattern, text)
            if not match:
                break
            guest = collapse(raw_guest)
            if guest and len(guest) < 80:
                guests.append(guest)
            text = text[:match.start()] + " " + text[match.end():]
    return collapse(text), guests


# Guion sin espacios alrededor: "Bob Marley-Sweat", "Charles Ans- Un Viaje".
# Partir por aqui a ciegas romperia "Spider-Man", asi que solo se acepta si un
# lado resulta ser el artista que ya conocemos.
TIGHT_SPLIT_RE = re.compile(r"\s*[-–—]\s*")


def strip_artist_prefix(title, artist, threshold=0.6):
    """Deshace los titulos con forma 'Artista - Titulo'.

    Devuelve (titulo, prefijo_retirado). El prefijo se devuelve porque a menudo
    lista colaboradores que el tag `artist` no tiene.
    """
    if not title or not artist:
        return title, ""
    parts = SPLIT_RE.split(title, maxsplit=1)
    if len(parts) != 2:
        parts = [x for x in TIGHT_SPLIT_RE.split(title, maxsplit=1)]
        if len(parts) != 2 or min(len(x.strip()) for x in parts) < 2:
            return title, ""
    left, right = parts[0].strip(), parts[1].strip()
    if not right:
        return title, ""
    # Se compara sin los parentesis: ahi van sellos, versiones y ruido de
    # descarga, y su parecido casual con el artista decide mal el orden.
    def parecido(lado):
        limpio = BRACKETED_RE.sub(" ", lado)
        # Una errata no cambia de quien se trata. "Gustavo Lima - Balada Boa"
        # con el tag "Gusttavo Lima" comparte una sola palabra de dos, se
        # quedaba en 0,5 y el titulo conservaba el nombre del cantante dentro.
        if same_text(limpio, artist):
            return 1.0
        return overlap(limpio, artist)

    left_score = parecido(left)
    right_score = parecido(right)
    # Forma "Titulo - Artista": el artista va detras, no delante.
    if right_score > left_score and right_score >= threshold:
        return left, right
    if left_score >= threshold:
        return right, left
    return title, ""


def strip_artist_affix(title, artist, min_len=4):
    """Quita el artista pegado al titulo cuando no hay separador ninguno.

    "la champions liga besame" y "solo quiero amarte la champions liga" traen el
    nombre del grupo delante o detras, sin guion ni nada. Solo se corta si el
    trozo coincide exactamente con el artista y queda titulo de sobra.
    """
    if not title or not artist or len(collapse(artist)) < min_len:
        return title
    words = title.split()
    if len(words) < 2:
        return title
    objetivo = norm(artist)
    if not objetivo:
        return title
    for size in range(len(words) - 1, 0, -1):
        if norm(" ".join(words[:size])) == objetivo:
            resto = collapse(" ".join(words[size:]))
            if resto:
                return resto
        if norm(" ".join(words[-size:])) == objetivo:
            resto = collapse(" ".join(words[:-size]))
            if resto:
                return resto
    return title


def strip_channel_tail(title, channel):
    """Quita del final del titulo el trozo que nombra al canal.

    "Wait - Warner Vault" con el tag de artista "Warner Records Vault" se queda
    en "Wait". No hace falta que el nombre coincida entero: quien sube el video
    lo abrevia en el titulo y lo escribe largo en el tag, o al reves.

    Se exige que quede titulo de sobra, para no vaciar una cancion que
    justamente se llame como el sello.
    """
    if not title or not channel:
        return title
    partes = [x.strip() for x in SPLIT_RE.split(title) if x.strip()]
    if len(partes) < 2:
        partes = [x.strip() for x in PIPE_RE.split(title) if x.strip()]
    if len(partes) < 2:
        return title
    while len(partes) >= 2 and overlap(partes[-1], channel) >= 0.6:
        partes = partes[:-1]
    resto = collapse(" - ".join(partes))
    return resto or title


def richer_artist(artist, prefix):
    """Se queda con el prefijo del titulo si nombra a todos los del tag y a alguien mas."""
    if not prefix:
        return artist
    ta, tp = tokens(artist), tokens(prefix)
    if ta and ta < tp:
        return prefix
    return artist


def merge_featured(artist, guests):
    if not guests:
        return artist
    existing = tokens(artist)
    extra = [g for g in guests if not tokens(g) <= existing]
    if not extra:
        return artist
    return "%s feat. %s" % (artist, ", ".join(extra))


def clean_date(value):
    text = (value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if match:
        return "%s-%s-%s" % match.groups()
    match = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        return "%s-%02d-%02d" % (match.group(1), int(match.group(2)), int(match.group(3)))
    match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", text)
    if match:
        return match.group(1)
    return ""


def clean_track_number(value):
    match = re.match(r"^\s*(\d{1,3})", value or "")
    return str(int(match.group(1))) if match else ""


def clean_pair(raw_artist, raw_title):
    """Limpieza completa de un par (artista, titulo) crudo.

    El orden importa: los invitados se extraen antes de deshacer el prefijo
    "Artista - Titulo", porque suelen ir incrustados en ese prefijo.
    """
    artist = clean_artist(raw_artist)
    title, guests = extract_featured(raw_title or "")
    title, prefix = strip_artist_prefix(title, artist)
    artist = richer_artist(artist, prefix)
    title = collapse(sub_folded(PROD_RE, "", strip_noise(title)))
    title = strip_artist_affix(title, artist)
    if not title:
        title = collapse(strip_noise(raw_title or ""))
    return merge_featured(artist, guests), title


COLLAB_SPLIT_RE = re.compile(
    r"\s*,\s*|\s+(?:feat|ft|featuring|con)\.?\s+|\s+x\s+|\s*&\s*(?=\w+\s*,)",
    re.IGNORECASE)


def main_artist(artist):
    """Artista principal, para el campo albumartist.

    Se corta en la coma, en el feat. y en la " x " de colaboracion, pero NO en
    "&": "Zion & Lennox" o "Capital Bra & Samra" son un solo acto, no dos.
    """
    if not artist:
        return ""
    first = COLLAB_SPLIT_RE.split(artist, maxsplit=1)[0]
    return collapse(first) or collapse(artist)


CREDIT_SPLIT_RE = re.compile(
    r"\s*,\s*|\s*&\s*|\s+(?:feat|ft|featuring|con|vs|versus)\.?\s+|\s+x\s+",
    re.IGNORECASE)


def artist_names(credit):
    """Parte un credito en los nombres sueltos que lo componen."""
    parts = [collapse(p) for p in CREDIT_SPLIT_RE.split(credit or "")]
    return [p for p in parts if p]


def _has_distinctive_case(value):
    """La grafia parece una decision del artista y no un Title Case comun."""
    words = re.findall(r"[^\W\d_]+", value or "", flags=re.UNICODE)
    for word in words:
        if len(word) >= 2 and word.isupper():
            return True
        # LiL, iDKHOW y grafias similares no son ni minusculas, ni MAYUSCULAS,
        # ni la capitalizacion corriente de una palabra.
        if (len(word) >= 3 and not word.islower() and not word.isupper()
                and word != word[:1].upper() + word[1:].lower()):
            return True
    return False


def preserve_artist_case(credit, references=()):
    """Conserva grafias deliberadas que ya aparecen en una pista local.

    Las fuentes no coinciden en nombres estilizados: unas devuelven BABYMETAL
    y otras Babymetal. Si carpeta, archivo o tags ya contienen una escritura
    claramente deliberada, se reutiliza solo para el mismo nombre; nunca sirve
    para decidir que el artista es otro.
    """
    result = collapse(credit or "")
    if not result:
        return result

    for name in artist_names(result):
        pattern = re.compile(r"(?<!\w)%s(?!\w)" % re.escape(name),
                             re.IGNORECASE | re.UNICODE)
        chosen = ""
        for reference in references:
            match = pattern.search(reference or "")
            if match and _has_distinctive_case(match.group(0)):
                chosen = match.group(0)
                break
        if chosen and chosen != name:
            result = pattern.sub(lambda _match, value=chosen: value,
                                 result, count=1)
    return result


def credit_matches(candidate, query, threshold=0.6):
    """Hay algun artista comun entre los dos creditos?

    Comparar los creditos enteros como un bloque falla por los dos lados: rechaza
    "Anuel AA, Daddy Yankee & KAROL G" cuando busco "Anuel AA", y acepta
    "Nirvana the Band the Show" cuando busco "Nirvana". Comparando nombre a
    nombre, la colaboracion pasa y el impostor no.
    """
    if not query:
        return True
    left = artist_names(candidate) or [candidate or ""]
    right = artist_names(query) or [query]
    for a in left:
        for b in right:
            if jaccard(a, b) >= threshold:
                return True
            # Una letra de mas o de menos no cambia de quien se trata:
            # "Damian Marleys" es "Damian Marley" con la s del plural pegada, y
            # comparando palabra por palabra no se parecian en nada.
            if same_text(a, b):
                return True

    # El credito del archivo trae varios artistas pegados sin separador:
    # "David Guetta Bebe Rexha". La fuente responde "David Guetta" y nombre a
    # nombre no llegan al umbral (jaccard 0,5). Que TODAS las palabras del
    # candidato esten en la consulta si lo confirma.
    #
    # Solo en ese sentido. Al reves seria aceptar "Nirvana the Band the Show"
    # cuando busco "Nirvana", que es justo el impostor que hay que rechazar.
    # Se exigen dos palabras: con una sola, "Bad" pasaria por "Bad Bunny".
    tc, tq = tokens(candidate), tokens(query)
    if len(tc) >= 2 and tc < tq:
        return True
    return False


def bare_title(title):
    """Titulo sin parentesis ni invitados, para comparar."""
    text = BRACKETED_RE.sub(" ", title or "")
    text = re.split(r"\s+(?:feat|ft|featuring)\.?\s+", text, flags=re.IGNORECASE)[0]
    return collapse(text)


def title_matches(candidate, query, threshold=0.5):
    """Compara titulos ignorando parentesis y feat."""
    a, b = bare_title(candidate), bare_title(query)
    if not a or not b:
        return False
    if norm(a) == norm(b):
        return True
    if jaccard(a, b) >= threshold:
        return True
    return same_text(a, b)


PIPE_RE = re.compile(r"\s*[|\uFF5C]\s*")


def split_pipe_artist(artist, reference=""):
    """Un artista con "|" casi siempre trae el canal o el sello pegado.

    "Luciano | Locosquad", "Dragostea Din Tei | Napalm Records": la barra sale
    del "_" que Windows dejo en el nombre del archivo. Se elige la parte que
    mejor case con el nombre del archivo, y si no hay con que comparar, la
    primera, que es donde suele ir el artista de verdad.
    """
    if not artist or not PIPE_RE.search(artist):
        return artist
    partes = [collapse(x) for x in PIPE_RE.split(artist) if collapse(x)]
    if not partes:
        return artist
    if reference:
        mejor = max(partes, key=lambda x: overlap(x, reference))
        if overlap(mejor, reference) > 0:
            return mejor
    return partes[0]


# ---------------------------------------------------------------------------
# Variantes de busqueda
#
# Todo lo que hay aqui abajo sirve SOLO para preguntar a las fuentes, nunca
# para decidir que se escribe en el tag. Un titulo recortado a la brava
# encuentra la cancion, pero el titulo bueno es el que devuelve la fuente, no
# el recorte. Asi se puede ser agresivo buscando sin estropear nada.
# ---------------------------------------------------------------------------

# Ruido que puede aparecer en MEDIO del titulo, no solo al final:
# "Stay With Me HD (Club Mix)". La lista es corta a proposito: son palabras que
# tambien existen dentro de titulos de verdad ("Video Games", "Radio Ga Ga"),
# y por eso esto no toca el titulo que se guarda.
INLINE_NOISE_RE = re.compile(
    r"(?<!\w)(?:hd|hq|4k|8k|1080p?|720p?|320\s*kbps|full\s*hd|"
    r"official|oficial|officiel|offizielles?|lyrics?|letra|"
    r"audio\s+oficial|official\s+audio|official\s+video|video\s+oficial)"
    r"(?!\w)", re.IGNORECASE)

# Un parentesis cerrado con texto detras: "I'm good (Blue) I'm good yeah I'm
# feelin' alright", "Hayya Hayya (Better Together) FIFA World Cup 2022".
# Lo de despues del parentesis casi siempre es la letra pegada o el nombre del
# evento, no parte del titulo.
AFTER_BRACKET_RE = re.compile(r"^(.{2,}?[\)\]\}])\s*\S")

# Errata muy comun en nombres descargados: el DJ se llama A-Trak, no A-Track.
# Solo se corrige en la consulta; nunca se reescribe el titulo local con esto.
A_TRACK_RE = re.compile(r"\bA[\s-]?Track\b", re.IGNORECASE)


def fix_search_typos(title):
    """Corrige erratas conocidas solo en la cadena usada para consultar."""
    return A_TRACK_RE.sub("A-Trak", title or "")


def strip_inline_noise(title):
    """Quita palabras de ruido esten donde esten. Solo para buscar."""
    return collapse(sub_folded(INLINE_NOISE_RE, " ", title or ""))


def cut_after_bracket(title):
    """Se queda con lo que va hasta el primer parentesis cerrado."""
    match = AFTER_BRACKET_RE.match(title or "")
    return collapse(match.group(1)) if match else ""


def cut_at_repeat(title):
    """Corta donde el titulo empieza a repetirse.

    Los descargadores pegan un trozo de la letra detras del titulo, y el titulo
    vuelve a aparecer dentro: "I'm good (Blue) I'm good yeah I'm feelin'
    alright". La segunda aparicion marca donde acababa el titulo de verdad.
    """
    words = (title or "").split()
    plegado = [norm(w) for w in words]
    if len(words) < 4:
        return ""
    # De la repeticion mas larga a la mas corta: con dos palabras se dispara
    # demasiado facil ("Bum Bum Tam Tam").
    for size in range(min(4, len(words) // 2), 1, -1):
        head = plegado[:size]
        for start in range(size, len(words) - size + 1):
            if plegado[start:start + size] == head:
                return collapse(" ".join(words[:start]))
    return ""


def title_search_variants(title, limit=6):
    """Formas alternativas del titulo con que preguntar, de exacta a amplia."""
    salida, vistos = [], set()

    def anadir(texto):
        texto = collapse(texto or "")
        if not texto or len(texto) < 2:
            return
        clave = norm(texto)
        if not clave or clave in vistos:
            return
        vistos.add(clave)
        salida.append(texto)

    anadir(title)
    anadir(fix_search_typos(title))
    anadir(bare_title(title))
    anadir(cut_after_bracket(title))
    anadir(cut_at_repeat(title))
    limpio = strip_inline_noise(title)
    anadir(limpio)
    anadir(bare_title(limpio))
    # Ultimo recurso: las primeras palabras. Solo tiene sentido con un titulo
    # largo, y solo se usa cuando hay artista con que verificar la respuesta.
    palabras = bare_title(limpio).split()
    if len(palabras) > 5:
        anadir(" ".join(palabras[:4]))
    # Titulos bilingues pegados: "Umi no Mieru Machi A Town With An Ocean
    # View" contiene el nombre japones y el ingles. La cola suele ser la que
    # mejor reconocen los catalogos occidentales.
    if len(palabras) > 8:
        anadir(" ".join(palabras[-6:]))
    return salida[:limit]


def split_title_pair(title):
    """Devuelve (izquierda, derecha) si el titulo lleva un separador dentro.

    "Yeah Yeah Yeahs - Heads Will Roll ( A-Track Remix )" es un par artista y
    titulo metido entero en el campo del titulo. Vale la pena preguntarlo asi:
    ninguna fuente conoce el bloque completo, y las dos mitades sueltas si.
    """
    partes = SPLIT_RE.split(title or "", maxsplit=1)
    if len(partes) != 2:
        return "", ""
    izquierda, derecha = collapse(partes[0]), collapse(partes[1])
    if len(izquierda) < 2 or len(derecha) < 2:
        return "", ""
    return izquierda, derecha

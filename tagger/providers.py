"""Cadena de fuentes de metadatos.

Medido sobre 40 archivos de 8 carpetas, con las verificaciones ya afinadas:

    MusicBrainz  77% de aciertos, 5 exclusivos, 7,3 s por consulta
    Deezer       67%,             0 exclusivos, 0,7 s
    iTunes       67%,             0 exclusivos, 3,1 s

MusicBrainz es la que mas encuentra. En modo normal se para en la primera fuente
util; en consenso Deezer y Last.fm se preguntan primero en paralelo. Si no se
ponen de acuerdo entra MusicBrainz, y iTunes queda como ultimo desempate cuando
ya existe alguna respuesta. Asi las esperas no se suman y un archivo desconocido
no paga iTunes en cada variante.
"""

import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from .covers import LADO_CAA, CoverArtClient, url_en_grande
from .deezer import DeezerClient, duration_tolerance
from .itunes import ItunesClient
from .lastfm import LastfmClient, key_is_configured
from .musicbrainz import MusicBrainzClient, contact_is_configured
from .text import (artist_names, bare_title, collapse, credit_matches,
                   fix_search_typos, norm, same_text, split_title_pair,
                   title_search_variants, tokens)


# Sufijos de edicion que no cambian de que album se trata: "Un Verano Sin Ti"
# y "Un Verano Sin Ti (Deluxe)" son el mismo disco a efectos de comparar.
EDITION_RE = re.compile(
    r"\s*[-(\[]\s*(?:single|ep|deluxe|deluxe\s+edition|expanded(\s+edition)?|"
    r"bonus\s+track\s+version|remaster(ed)?(\s*\d{4})?|apple\s+music\s+edition|"
    r"special\s+edition|edicion\s+especial|version\s+deluxe|explicit|"
    r"anniversary\s+edition|official\s+soundtrack|\d{4}\s+remaster)"
    r"\s*[)\]]?\s*$",
    re.IGNORECASE)


# Palabras que delatan un recopilatorio, un directo o un disco de versiones.
# Se mira el titulo porque es lo unico que devuelven las tres fuentes por igual:
# el tipo de edicion solo lo da MusicBrainz, y solo a veces.
COMPILATION_MARKERS = (
    ("greatest hits", 4), ("grandes exitos", 4), ("best of", 4), ("lo mejor", 4),
    ("the very best", 4), ("top hits", 4), ("mega hits", 4), ("super hits", 4),
    ("hits", 2), ("exitos", 2), ("coleccion", 3), ("collection", 3),
    ("antologia", 3), ("anthology", 3), ("compilado", 3), ("compilation", 3),
    ("now that s what i call", 4), ("sus mejores", 3), ("essential", 2),
    ("live", 3), ("en vivo", 3), ("en directo", 3), ("unplugged", 2),
    ("karaoke", 6), ("tribute", 6), ("cover", 3), ("instrumental", 3),
    ("soundtrack", 2), ("bso", 2),
    ("chart", 4), ("top 40", 4), ("top40", 4), ("new year", 3),
    # Series numeradas de recopilatorios: "Nope Is Dope, Volume 13",
    # "Zooland 2018", "Ministry of Sound Sessions".
    ("volume", 3), ("vol ", 3), ("sessions", 2), ("mixed by", 3),
    ("presents", 2), ("selection", 2), ("compiled", 3), ("various", 3),
)


def compilation_penalty(album_title):
    """Cuanto huele a recopilatorio el titulo de un album. Cero es limpio."""
    text = norm(album_title)
    if not text:
        return 0
    penalty = 0
    for word, cost in COMPILATION_MARKERS:
        if word in text:
            penalty += cost
    # "15 Top Trance Hits 2012-02" y demas listados numerados.
    if re.match(r"^\d{1,3}\s", text):
        penalty += 2
    return penalty


def album_key(title):
    """Forma comparable de un titulo de album."""
    text = title or ""
    for _ in range(3):
        stripped = EDITION_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return norm(text)


# Cuantos pares (artista, titulo) se prueban como mucho por archivo. Sin tope,
# un tema que no conoce nadie se lleva veinte consultas y una carpeta entera
# tarda horas.
MAX_VARIANTS = 10
ARTWORK_MEMORY_ITEMS = 96
CIRCUIT_FAILURES = 3
CIRCUIT_SECONDS = 60

# Que fuentes se preguntan segun lo lejos que se este del dato original. Las
# primeras variantes son las de verdad y merecen las cuatro fuentes; las
# ultimas son recortes a la desesperada y solo justifican las rapidas.
#
# iTunes cuesta 3,1 s por consulta (Apple corta a las 20 por minuto) y en la
# muestra no encontro ni un tema que las otras no tuvieran: pagarlo diez veces
# por archivo es lo que convertia una carpeta grande en horas de espera.
SOURCES_EXACT = ("deezer", "musicbrainz", "lastfm", "itunes")
SOURCES_NEAR = ("deezer", "musicbrainz", "lastfm", "itunes")
SOURCES_FAR = ("deezer", "lastfm")


# El tamano que la URL anuncia. Deezer "/1000x1000-000000-80-0-0.jpg" o
# "/1000x", iTunes "600x600bb.jpg", Cover Art Archive ".../front-500".
_URL_SIZE_RES = (
    re.compile(r"/(\d{2,4})x(?:\d{2,4})?(?:[-./]|$)"),
    re.compile(r"(\d{2,4})x\1[a-z]{0,4}\.(?:jpg|jpeg|png)", re.IGNORECASE),
    re.compile(r"/front-(\d{2,4})\b", re.IGNORECASE),
)


def declared_side(url):
    """Lado que anuncia la URL, o 0 si no lo dice."""
    if not url:
        return 0
    for patron in _URL_SIZE_RES:
        encontrado = patron.search(url)
        if encontrado:
            try:
                return int(encontrado.group(1))
            except (TypeError, ValueError):
                return 0
    return 0


def _sources_for(index):
    if index < 2:
        return SOURCES_EXACT
    if index < 5:
        return SOURCES_NEAR
    return SOURCES_FAR


class Providers:
    def __init__(self, cache_dir, use_deezer=True, use_itunes=True,
                 use_musicbrainz=True, use_lastfm=True, min_score=90):
        self.deezer = DeezerClient(cache_dir / "deezer.json") if use_deezer else None
        self.itunes = ItunesClient(cache_dir / "itunes.json") if use_itunes else None

        # Last.fm es opcional: sin clave se desactiva sola y el resto sigue.
        self.lastfm = None
        if use_lastfm and key_is_configured():
            self.lastfm = LastfmClient(cache_dir / "lastfm.json")

        self.musicbrainz = None
        self.coverart = None
        if use_musicbrainz and contact_is_configured():
            self.musicbrainz = MusicBrainzClient(cache_dir / "musicbrainz.json",
                                                 min_score=min_score)
            self.coverart = CoverArtClient(cache_dir / "coverart.json",
                                           size=LADO_CAA)

        self.hits = {"deezer": 0, "itunes": 0, "musicbrainz": 0, "lastfm": 0}
        self.init_runtime_state()

    def init_runtime_state(self):
        """Contadores, hilos y caches de una ejecucion.

        Aparte del constructor porque las pruebas crean el objeto con __new__
        para no tocar la red: asi piden este mismo estado en una linea en vez de
        ir copiando a mano cada atributo nuevo.
        """
        self.misses = 0
        self.consensus = {1: 0, 2: 0, 3: 0, 4: 0}
        self.artwork_rescued = 0
        # El trabajo de varios archivos puede solaparse, pero cada servicio se
        # usa de uno en uno. Asi una espera de MusicBrainz no impide adelantar
        # Deezer/Last.fm para la cancion siguiente y se respetan sus limites.
        self._executor = ThreadPoolExecutor(max_workers=8,
                                            thread_name_prefix="fuentes")
        self._source_locks = {
            name: threading.RLock()
            for name in ("deezer", "itunes", "musicbrainz", "lastfm")
        }
        self._health_lock = threading.Lock()
        self._consecutive_failures = {name: 0 for name in self._source_locks}
        self._disabled_until = {name: 0.0 for name in self._source_locks}
        self.source_pauses = {name: 0 for name in self._source_locks}

        # Una portada pertenece al album, no a cada pista. Se conservan unas
        # pocas imagenes en RAM para que doce canciones del mismo disco no
        # descarguen doce veces los mismos bytes.
        self._artwork_cache = OrderedDict()
        self._artwork_lock = threading.RLock()
        self._artwork_key_locks = {}
        self.artwork_cache_hits = 0
        self.artwork_skipped = 0

    def _variantes(self, artist, title, hints=(), max_variants=None):
        """Pares (artista, titulo) a probar, del mas exacto al mas amplio.

        Buscar estrecho y verificar amplio. Deezer no conoce el credito
        "Young Mask x Rill", pero si conoce "Young Mask", y el resultado sigue
        pasando la verificacion contra el credito entero. Lo mismo con los
        titulos: "Panama [Musikvideo]" no existe, "Panama" si.

        El orden no es capricho: manda lo exacto, luego lo que dicen las otras
        pistas del archivo (la carpeta, el albumartist) y por ultimo los
        recortes del titulo. Se para en el primer acierto, asi que las
        canciones faciles siguen costando una sola consulta; solo las dificiles
        bajan la lista.
        """
        vistos = set()
        planes = []

        def anadir(art, tit):
            art, tit = collapse(art or ""), collapse(tit or "")
            if not tit or len(tit) < 2:
                return
            clave = (norm(art), norm(tit))
            if not clave[1] or clave in vistos:
                return
            vistos.add(clave)
            planes.append((art, tit))

        titulos = title_search_variants(title)
        principal = titulos[0] if titulos else title
        desnudo = bare_title(title)
        if norm(desnudo) == norm(principal):
            desnudo = ""

        nombres = [artist] if artist else []
        for nombre in artist_names(artist):
            if nombre and nombre not in nombres:
                nombres.append(nombre)

        # Pistas de fuera del campo de artista: la carpeta donde esta el
        # archivo y el albumartist. La carpeta "electro/david guetta" sabe
        # quien canta aunque el tag diga "David Guetta Bebe Rexha" y ninguna
        # fuente conozca ese credito pegado. Solo entran las que aporten un
        # nombre que no estuviera ya.
        pistas = []
        for pista in hints:
            pista = collapse(pista or "")
            if not pista or len(pista) < 3:
                continue
            if any(same_text(pista, n) for n in nombres):
                continue
            if pista not in pistas:
                pistas.append(pista)

        izquierda, derecha = split_title_pair(title)

        # 1. el credito entero, con el titulo tal cual y sin adornos
        anadir(artist, principal)
        anadir(artist, desnudo)
        # 2. cada nombre suelto del credito
        for nombre in nombres[1:]:
            anadir(nombre, principal)
        # 3. el titulo trae dentro "Artista - Titulo". "Project X - Yeah Yeah
        #    Yeahs - Heads Will Roll" deja el grupo metido en el titulo, y
        #    ninguna fuente conoce el bloque entero.
        if izquierda and derecha:
            anadir(izquierda, fix_search_typos(derecha))
            for tit in title_search_variants(derecha):
                anadir(izquierda, tit)
        # 4. la carpeta y el albumartist como artista
        for pista in pistas:
            for tit in titulos:
                anadir(pista, tit)
        # 5. los recortes del titulo, con el credito original
        for tit in titulos[2:]:
            anadir(artist, tit)
        # 6. el par de dentro del titulo, al reves
        if izquierda and derecha:
            anadir(derecha, izquierda)

        # A veces el supuesto artista es en realidad el nombre original y su
        # traduccion pegados ("Umi no Mieru Machi A Town With An Ocean View").
        # Un credito real rara vez supera siete palabras. Se prueba ese bloque
        # como titulo, sin artista, y solo se podra escribir automaticamente si
        # el consenso o la verificacion fuerte confirman el resultado.
        if len((artist or "").split()) > 7:
            titulos_del_credito = title_search_variants(artist)
            # Si la carpeta o albumartist aporta el autor real, combinalo con
            # el supuesto credito: en ripeos de video el titulo original y su
            # traduccion suelen haber acabado juntos en ARTIST.
            for pista in pistas:
                for tit in titulos_del_credito:
                    anadir(pista, tit)
            for tit in titulos_del_credito:
                anadir("", tit)

        # Ultimo recurso: sin artista, a texto libre. Sirve cuando el tag de
        # artista es un canal y el nombre del archivo no tiene separador con que
        # repartir ("Max Giesinger Wenn Sie Tanzt.mp3" con artista "Music Zone").
        # Se exige un titulo largo: sin artista que contrastar, uno corto como
        # "Eva" encajaria con cualquier cosa.
        for tit in titulos[:3]:
            if len(tit) >= 14:
                anadir("", tit)

        limit = MAX_VARIANTS if max_variants is None else max(1, max_variants)
        return planes[:limit]

    def _source_paused(self, name):
        with self._health_lock:
            return time.monotonic() < self._disabled_until.get(name, 0.0)

    def _source_result(self, name, failed):
        """Abre temporalmente el circuito tras fallos reales consecutivos."""
        with self._health_lock:
            if not failed:
                self._consecutive_failures[name] = 0
                return
            count = self._consecutive_failures.get(name, 0) + 1
            if count >= CIRCUIT_FAILURES:
                self._disabled_until[name] = time.monotonic() + CIRCUIT_SECONDS
                self._consecutive_failures[name] = 0
                self.source_pauses[name] += 1
            else:
                self._consecutive_failures[name] = count

    @staticmethod
    def _merece_probar_cambio(artist, title):
        """Vale la pena preguntar por el par al reves?

        Solo si los dos campos existen, son cortos como para ser un nombre y no
        son el mismo texto. Asi la sonda cuesta una consulta extra solo en los
        archivos que ya han fallado, no en los que van bien.
        """
        if not artist or not title:
            return False
        if len(artist.split()) > 5 or len(title.split()) > 5:
            return False
        return not same_text(artist, title)

    # ------------------------------------------------------------- consultas
    def _ask(self, name, art, tit, duration, want_date):
        """Una consulta a una fuente. Devuelve el dict o None; nunca revienta."""
        client = getattr(self, name, None)
        if client is None or self._source_paused(name):
            return None
        with self._source_locks[name]:
            if self._source_paused(name):
                return None
            errors_before = client.errors
            try:
                if name == "deezer":
                    match = client.lookup(art, tit, duration=duration,
                                          want_date=want_date)
                elif name == "musicbrainz":
                    match = client.lookup(art, tit)
                    if match:
                        match = dict(match)
                        match["source"] = "musicbrainz"
                        match.setdefault("artwork_url", "")
                else:
                    match = client.lookup(art, tit)
            except Exception:
                client.errors += 1
                match = None
            self._source_result(
                name, not match and client.errors > errors_before)
        if match:
            with self._health_lock:
                self.hits[name] += 1
        return match

    def _ask_parallel(self, names, art, tit, duration, want_date):
        """Pregunta a varias fuentes a la vez.

        Son servicios independientes y cada cliente lleva su propio freno: la
        espera de uno no tiene por que sumarse a la del otro. En fila, una
        variante costaba 0,25 + 3,1 + 1 + 0,25 s; a la vez cuesta lo que tarde
        la mas lenta de las que se pregunten.
        """
        activos = [n for n in names if getattr(self, n, None) is not None]
        if not activos:
            return []
        if len(activos) == 1:
            match = self._ask(activos[0], art, tit, duration, want_date)
            return [match] if match else []
        futuros = [self._executor.submit(
            self._ask, n, art, tit, duration, want_date) for n in activos]
        respuestas = [f.result() for f in futuros]
        return [m for m in respuestas if m]

    @staticmethod
    def _hay_acuerdo(matches):
        """Hay ya dos fuentes distintas diciendo el mismo album?"""
        albumes = {}
        for match in matches:
            if match.get("album"):
                albumes.setdefault(album_key(match["album"]), set()).add(
                    match.get("source", ""))
        return any(len(fuentes) >= 2 for fuentes in albumes.values())

    def lookup(self, artist, title, duration=None, want_date=True, hints=(),
               max_variants=None):
        """Primera fuente que responda. Devuelve el dict con 'source', o None.

        want_date=False ahorra una llamada por acierto en Deezer: la fecha de
        publicacion exige consultar el album aparte, y para las portadas no hace
        falta.

        Si no aparece nada, se prueba con el artista y el titulo intercambiados.
        Cuando el descargador metio el titulo en el campo de artista Y el nombre
        del archivo esta en el mismo orden equivocado, no hay ninguna pista
        local que lo delate: "Welcome to Jamrock - Damian Marleys" se ve bien
        por los dos lados. Las fuentes si lo saben, porque solo una de las dos
        combinaciones existe.
        """
        if not title:
            return None

        match = self._first_hit(artist, title, duration, want_date, hints,
                                max_variants)
        if match:
            return match
        if self._merece_probar_cambio(artist, title):
            match = self._first_hit(title, artist, duration, want_date,
                                    max_variants=max_variants)
            if match:
                match = dict(match)
                match["swapped"] = True
                return match

        self.misses += 1
        return None

    def _first_hit(self, artist, title, duration=None, want_date=True, hints=(),
                   max_variants=None):
        titulos_del_artista = {
            norm(t) for t in title_search_variants(artist)
        } if len((artist or "").split()) > 7 else set()
        for indice, (art, tit) in enumerate(
                self._variantes(artist, title, hints, max_variants)):
            # En el intento a texto libre no se manda artista, asi que la fuente
            # no lo verifica. Hay que hacerlo aqui contra el original, o
            # "Digimon - Staffel 1 - Opening" acaba con la portada de un disco
            # de K-pop solo porque el titulo se parecia.
            def valido(m, _art=art):
                if _art or not artist:
                    return True
                if norm(tit) in titulos_del_artista:
                    return True
                return credit_matches(m.get("artist", ""), artist)

            for name in _sources_for(indice):
                match = self._ask(name, art, tit, duration, want_date)
                # Last.fm puede reconocer el texto pero devolver album e imagen
                # vacios. Eso no resuelve nada y, peor, antes impedia probar la
                # siguiente variante limpia del titulo.
                if (match and valido(match)
                        and (match.get("album") or match.get("artwork_url"))):
                    return match

        return None

    def consensus_lookup(self, artist, title, duration=None, want_date=True,
                         hints=(), max_variants=None):
        """Compara las fuentes disponibles y sus respuestas.

        Que dos fuentes independientes digan el mismo album es una garantia muy
        distinta a que lo diga una sola. Con acuerdo el resultado sale como
        "alta" y se escribe. Sin acuerdo se mira lo exacta que es la
        coincidencia: si el titulo es identico, el artista casa y la duracion
        cuadra, sale como "media" y tambien se escribe; si solo se parece, sale
        como "revisar" y se queda para que lo mires.
        """
        if not title:
            return None

        found = self._gather(artist, title, duration, want_date, hints,
                             max_variants)
        swapped = False
        if not found and self._merece_probar_cambio(artist, title):
            found = self._gather(title, artist, duration, want_date,
                                 max_variants=max_variants)
            swapped = bool(found)
        if not found:
            self.misses += 1
            return None
        return self._consensus(found, swapped, artist, title, duration)

    def _gather(self, artist, title, duration=None, want_date=True, hints=(),
                max_variants=None):
        """Todas las respuestas de todas las fuentes para un par dado.

        Las fuentes de cada variante se preguntan a la vez. Una respuesta sin
        album o apenas parecida se conserva como plan B, pero no impide probar
        la siguiente variante limpia. iTunes solo entra si ya hay una respuesta
        y las rapidas no se han puesto de acuerdo: vale como voto de desempate,
        no como sonda lenta a ciegas.
        """
        found = []
        titulos_del_artista = {
            norm(t) for t in title_search_variants(artist)
        } if len((artist or "").split()) > 7 else set()
        for indice, (art, tit) in enumerate(
                self._variantes(artist, title, hints, max_variants)):
            def valido(m, _art=art):
                if _art or not artist:
                    return True
                if norm(tit) in titulos_del_artista:
                    return True
                return credit_matches(m.get("artist", ""), artist)

            fuentes = _sources_for(indice)

            # Deezer y Last.fm suelen responder en una fraccion de segundo. Si
            # ambos ya coinciden, MusicBrainz no puede cambiar el ganador de
            # consenso (seria 2 contra 1 como maximo), asi que se evita su
            # espera respetando exactamente el mismo umbral de dos fuentes.
            primarias = [f for f in ("deezer", "lastfm") if f in fuentes]
            respuestas = [m for m in self._ask_parallel(
                primarias, art, tit, duration, want_date) if valido(m)]

            if ("musicbrainz" in fuentes
                    and not self._hay_acuerdo(respuestas)):
                match = self._ask(
                    "musicbrainz", art, tit, duration, want_date)
                if match and valido(match):
                    respuestas.append(match)

            # iTunes es desempate, no una sonda a ciegas: si ninguna fuente
            # rapida encontro nada, pagar sus 3,1 s no aporta evidencia y hace
            # que carpetas grandes parezcan colgadas.
            if ("itunes" in fuentes and respuestas
                    and not self._hay_acuerdo(respuestas)):
                match = self._ask("itunes", art, tit, duration, want_date)
                if match and valido(match):
                    respuestas.append(match)

            respuestas = [m for m in respuestas if m.get("album")]
            if not respuestas:
                continue

            # Una respuesta a una consulta ruidosa puede ser apenas parecida y
            # no debe bloquear las variantes siguientes. Se conserva como plan
            # B, pero solo se para si dos fuentes coinciden en el album o si una
            # respuesta verifica exactamente el par concreto que se pregunto.
            preparadas = []
            for match in respuestas:
                copia = dict(match)
                copia["_query_strong"] = self._coincidencia_exacta(
                    copia, art, tit, duration)
                preparadas.append(copia)
            if not found:
                found = preparadas
            if (self._hay_acuerdo(preparadas)
                    or any(m["_query_strong"] for m in preparadas)):
                found = preparadas
                break

        return found

    def _coincidencia_exacta(self, match, artist, title, duration):
        """Basta una sola fuente para esto?

        Con dos fuentes de acuerdo no hay duda. Con una sola, la garantia tiene
        que venir de lo ajustada que sea la respuesta: el titulo identico letra
        por letra (no parecido), el artista confirmado y la duracion dentro del
        margen cuando la fuente publica esa duracion. "Nunca Te Voy A Olvidar"
        de A.B. Quintanilla solo la conoce MusicBrainz: dejarla sin album pese a
        titulo, artista y score exactos era perder un dato bueno.
        """
        if not title:
            return False
        if norm(bare_title(match.get("title", ""))) != norm(bare_title(title)):
            return False
        if artist and not credit_matches(match.get("artist", ""), artist):
            return False
        propia = match.get("duration")
        # Sin artista hacen falta las otras dos pruebas: titulo exacto Y una
        # duracion publicada por la fuente. Asi se rescatan soundtracks claros
        # sin convertir una coincidencia textual vaga en autor inventado.
        if not artist and not (duration and propia):
            return False
        if duration and propia:
            diferencia = abs(float(propia) - duration)
            # Cuando no hay artista, la duracion es el unico segundo testigo.
            # El margen general admite masters con silencios distintos, pero
            # aqui seria suficiente para confundir canciones homonimas (como
            # las distintas "Umi no Mieru Machi").
            margen = (min(4.0, duration_tolerance(duration))
                      if not artist else duration_tolerance(duration))
            if diferencia > margen:
                return False
        return True

    def _consensus(self, found, swapped=False, artist="",
                   title="", duration=None):
        groups = {}
        for match in found:
            groups.setdefault(album_key(match["album"]), []).append(match)

        # Gana el album mas votado. A igualdad de votos, el que menos huele a
        # recopilatorio: sin esto se colaban cosas como "15 Top Trance Hits" o
        # "The Story of Hardwell (Best Of)" por delante del disco de verdad.
        winner = min(groups.values(),
                     key=lambda group: (-len(group),
                                        compilation_penalty(group[0]["album"])))
        agree = len(winner)
        sources = [m["source"] for m in winner]

        # De las respuestas que coinciden se toma el nombre de album mas corto,
        # que suele venir sin el "- Single" ni el "(Deluxe)".
        best = min(winner, key=lambda m: len(m["album"]))
        result = dict(best)

        # La API que aporta el nombre de album mas limpio no siempre devuelve
        # el credito completo. En una colaboracion se conserva el credito mas
        # informativo del grupo ganador: iTunes conoce "David Guetta & Bebe
        # Rexha" donde Last.fm solo responde "David Guetta".
        richer_credit = max(winner,
                            key=lambda m: len(tokens(m.get("artist", ""))))
        if richer_credit.get("artist"):
            result["artist"] = richer_credit["artist"]

        date = ""
        for match in sorted(winner, key=lambda m: m.get("date") or "9999"):
            if match.get("date"):
                date = match["date"]
                break
        result["date"] = date

        # La portada se elige por la CANCION, no por el disco. Si alguna fuente
        # conoce una edicion que se llama como el tema, esa es la caratula de
        # esta cancion; la del album es la misma para las doce que lleva dentro.
        #
        # La unica edicion ajena al album ganador que se admite es esa: la que
        # SE LLAMA como el tema. Asi entra el single "Komet" cuando el disco es
        # "Gartenstadt", y no entra "Harder Better Faster Stronger" cuando se
        # busca "Veridis Quo", que es como acabo una cancion con la caratula de
        # otra.
        propias = []
        if title:
            clave_tema = album_key(title)
            for match in found:
                if not match.get("album") or match in winner:
                    continue
                if album_key(match["album"]) != clave_tema:
                    continue
                if compilation_penalty(match["album"]):
                    continue
                if match.get("artwork_url") or match.get("release_id"):
                    propias.append(match)

        candidatas = []
        for match in propias + winner:
            if match.get("artwork_url"):
                candidatas.append({
                    "source": match["source"],
                    "url": match["artwork_url"],
                    "release_id": match.get("release_id", ""),
                    "release_group_id": match.get("release_group_id", ""),
                })
            elif match.get("source") == "musicbrainz" and (
                    match.get("release_id") or match.get("release_group_id")):
                candidatas.append({
                    "source": "musicbrainz", "url": "",
                    "release_id": match.get("release_id", ""),
                    "release_group_id": match.get("release_group_id", ""),
                })
        orden = {"deezer": 0, "itunes": 1, "musicbrainz": 2}
        candidatas.sort(key=lambda c: orden.get(c["source"], 9))
        result["artwork_candidates"] = candidatas
        if candidatas:
            result["artwork_source"] = candidatas[0]["source"]
            result["artwork_url"] = candidatas[0]["url"]

        result["source"] = "+".join(sources) if agree > 1 else sources[0]
        result["agree"] = agree
        if swapped:
            result["swapped"] = True
        query_strong = any(m.get("_query_strong") for m in winner)
        if agree >= 2:
            result["confidence"] = "alta"
        elif (query_strong
              or self._coincidencia_exacta(result, artist, title, duration)):
            result["confidence"] = "media"
        else:
            result["confidence"] = "revisar"
        result.pop("_query_strong", None)
        # El genero se toma de cualquiera de las fuentes que lo traiga, aunque no
        # sea la que gano el album: MusicBrainz acierta mucho pero nunca da
        # genero, e iTunes lo da siempre.
        generos = []
        for match in found:
            for nombre in (match.get("genres") or []):
                if nombre not in generos:
                    generos.append(nombre)
        result["genres"] = generos

        result["compilation_penalty"] = compilation_penalty(result["album"])
        winning_key = album_key(best["album"])
        result["alternatives"] = sorted({
            m["album"] for key, group in groups.items() if key != winning_key
            for m in group
        })
        self.consensus[min(agree, 4)] += 1
        return result

    def fetch_artwork(self, match, best=True, min_side_wanted=0):
        """Bytes de la portada. Con best=True prueba todas y elige la mejor.

        "Mejor" es la de mayor lado que ademas sea cuadrada: una caratula de
        album lo es, y lo que no lo es suele ser una miniatura de video.

        min_side_wanted evita descargas inutiles. Si el archivo ya tiene una
        portada correcta de 1000 px, una candidata que la propia URL anuncia
        como de 600 no se va a usar pase lo que pase: si resulta ser la misma
        foto no mejora nada, y si es otra imagen se conserva la que hay. Se
        devuelve None sin gastar la descarga.
        """
        if not match:
            return None

        from .covers import bad_cover, min_side

        candidatas = match.get("artwork_candidates")
        if not candidatas:
            candidatas = [(match.get("artwork_source") or match.get("source"),
                           match.get("artwork_url") or "")]

        mejor, mejor_lado = None, -1
        for candidata in candidatas:
            if isinstance(candidata, dict):
                fuente = candidata.get("source", "")
                url = candidata.get("url", "")
                release_id = candidata.get("release_id", "")
                release_group_id = candidata.get("release_group_id", "")
            else:
                # Compatibilidad con el formato transitorio (fuente, URL).
                fuente, url = candidata
                release_id = release_group_id = ""
            url = url_en_grande(url)
            if min_side_wanted:
                anunciado = declared_side(url)
                if anunciado and anunciado <= min_side_wanted:
                    self.artwork_skipped += 1
                    continue
            datos = self._fetch_one(match, fuente, url, release_id,
                                    release_group_id)
            if not datos:
                continue
            lado = min_side(datos)
            if bad_cover(datos):
                # Sirve como ultimo recurso, pero no gana a una buena.
                if mejor is None:
                    mejor, mejor_lado = datos, 0
                continue
            if lado > mejor_lado:
                mejor, mejor_lado = datos, lado
                if not best:
                    break

        # Ninguna candidata sirvio, pero el disco esta identificado: se busca la
        # portada por el nombre del album. Es lo que salva a los temas que solo
        # conoce Last.fm, que ya no sirve imagenes, y a los de MusicBrainz cuyo
        # Cover Art Archive esta vacio.
        if (mejor is None or mejor_lado <= 0) and not min_side_wanted:
            rescatada = self._album_artwork(match)
            if rescatada and not bad_cover(rescatada):
                return rescatada
            if rescatada and mejor is None:
                mejor = rescatada
        return mejor

    def _album_artwork(self, match):
        """Portada buscada por artista y nombre del disco, no por la cancion."""
        album = match.get("album") or ""
        artist = match.get("artist") or ""
        if not album:
            return None
        for source, cliente in (("deezer", self.deezer),
                                ("itunes", self.itunes)):
            if cliente is None or not hasattr(cliente, "album_artwork"):
                continue
            with self._source_locks[source]:
                url = cliente.album_artwork(artist, album)
            if not url:
                continue
            datos = self._fetch_one(match, source, url)
            if datos:
                with self._health_lock:
                    self.artwork_rescued += 1
                return datos
        return None

    def _cached_artwork(self, key, loader):
        """Evita descargar dos veces la misma imagen durante una ejecucion."""
        with self._artwork_lock:
            if key in self._artwork_cache:
                value = self._artwork_cache[key]
                self._artwork_cache.move_to_end(key)
                self.artwork_cache_hits += 1
                return value
            key_lock = self._artwork_key_locks.setdefault(
                key, threading.Lock())

        # Dos canciones del mismo album pueden resolverse a la vez. Solo una
        # descarga; la otra espera unos instantes y recoge los mismos bytes.
        with key_lock:
            with self._artwork_lock:
                if key in self._artwork_cache:
                    value = self._artwork_cache[key]
                    self._artwork_cache.move_to_end(key)
                    self.artwork_cache_hits += 1
                    return value
            value = loader()
            with self._artwork_lock:
                self._artwork_cache[key] = value
                self._artwork_cache.move_to_end(key)
                while len(self._artwork_cache) > ARTWORK_MEMORY_ITEMS:
                    self._artwork_cache.popitem(last=False)
                self._artwork_key_locks.pop(key, None)
            return value

    def _fetch_one(self, match, source, url, release_id="", release_group_id=""):
        url = url_en_grande(url)
        release_id = release_id or match.get("release_id", "")
        release_group_id = (release_group_id
                            or match.get("release_group_id", ""))
        key = (source or "", url or "", release_id or "",
               release_group_id or "")

        def download():
            lock = self._source_locks.get(source, self._artwork_lock)
            with lock:
                if source == "deezer" and self.deezer and url:
                    return self.deezer.fetch_artwork(url)
                if source == "itunes" and self.itunes and url:
                    return self.itunes.fetch_artwork(url)
                if source == "lastfm" and self.lastfm and url:
                    return self.lastfm.fetch_artwork(url)
                if source == "musicbrainz" and self.coverart:
                    return self.coverart.fetch(release_id, release_group_id)
                return None

        if not any(key[1:]):
            return None
        return self._cached_artwork(key, download)

    def summary(self):
        lines = ["  Aciertos por fuente:"]
        for name in ("deezer", "itunes", "musicbrainz", "lastfm"):
            client = getattr(self, name)
            if client is None:
                continue
            lines.append("    %-12s %5d aciertos, %4d consultas, %4d cache, %d errores"
                         % (name, self.hits[name], client.requests,
                            client.cache_hits, client.errors))
        lines.append("    %-12s %5d" % ("sin nada", self.misses))
        if any(self.consensus.values()):
            lines.append("  Consenso:")
            lines.append("    %-12s %5d  (se escriben)"
                         % ("2 o mas de acuerdo",
                            self.consensus[2] + self.consensus[3]
                            + self.consensus[4]))
            lines.append("    %-12s %5d  (quedan para revisar)"
                         % ("una sola fuente", self.consensus[1]))
        if self.coverart:
            lines.append("    %-12s %5d imagenes, %d errores"
                         % ("coverart", self.coverart.downloads, self.coverart.errors))
        if self.artwork_rescued:
            lines.append("    %-12s %5d portadas rescatadas por nombre de album"
                         % ("por album", self.artwork_rescued))
        if self.artwork_cache_hits:
            lines.append("    %-12s %5d descargas repetidas evitadas"
                         % ("cache imagen", self.artwork_cache_hits))
        pauses = sum(self.source_pauses.values())
        if pauses:
            lines.append("    %-12s %5d pausas automaticas por fallos de red"
                         % ("red", pauses))
        return "\n".join(lines)

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=False)
        for client in (self.deezer, self.itunes, self.musicbrainz,
                       self.lastfm, self.coverart):
            if client is not None:
                client.close()

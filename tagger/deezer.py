"""Busqueda en Deezer. Sin clave, sin registro.

Es la mas rapida de las tres con diferencia (0,7 s por consulta frente a los
7,3 s de MusicBrainz), por eso se consulta primero. En aciertos queda por
detras: 67% frente al 77% de MusicBrainz sobre la misma muestra de 40 archivos,
y no encontro ninguna cancion que MusicBrainz no tuviera.

Devuelve la duracion de la pista, asi que se puede contrastar con la del
archivo: es la senal mas fuerte para no confundir una version con otra.
"""

import json
import time
import urllib.parse
from pathlib import Path

import requests

from .text import clean_date, credit_matches, norm, title_matches

SEARCH = "https://api.deezer.com/search"
ALBUM = "https://api.deezer.com/album/%s"

# Deezer admite unas 50 peticiones cada 5 segundos. Se va muy por debajo.
MIN_INTERVAL = 0.25

# Margen para dar por buena la duracion. Ocho segundos fijos rechazaban
# aciertos evidentes: "Veridis Quo" dura 345 s en Deezer y 357 s en el MP3
# descargado de YouTube, porque el video lleva silencio o intro pegada. El
# margen tiene que crecer con la cancion: doce segundos o el 6%, lo que sea
# mayor.
DURATION_TOLERANCE = 12.0
DURATION_FRACTION = 0.06


def duration_tolerance(duration):
    return max(DURATION_TOLERANCE, abs(duration or 0) * DURATION_FRACTION)

# Tipos de edicion que no interesan como album de la cancion.
RECORD_PENALTY = {"album": 0, "ep": 1, "single": 1, "compile": 4}


COMPILATION_WORDS = (
    "lo mejor", "grandes exitos", "greatest hits", "best of", "the very best",
    "coleccion", "collection", "antologia", "anthology", "exitos", "hits",
    "vol ", "vol.", "compilado", "mix", "sus mejores",
)


def _compilation_penalty(item):
    """Penaliza recopilatorios y directos por el titulo del album.

    Mirar el record_type de verdad exigiria una llamada por candidato; el titulo
    delata a la mayoria y sale gratis.
    """
    title = norm((item.get("album") or {}).get("title") or "")
    penalty = 0
    for word in COMPILATION_WORDS:
        if word.strip() in title:
            penalty += 2
    for word in ("live", "en vivo", "en directo", "karaoke", "tribute",
                 "instrumental", "cover"):
        if word in title:
            penalty += 3
    return penalty


# Version de la logica de busqueda. Los fallos ("no existe") se guardan con esta
# marca, y al subirla se reintentan todos: si no, una mejora en la busqueda no
# sirve de nada porque la cache sigue afirmando que la cancion no existe.
SEARCH_VERSION = 5

MISS = "_miss"

class DeezerClient:
    def __init__(self, cache_path, timeout=20, max_retries=3):
        self.path = Path(cache_path)
        self.data = {}
        self.dirty = 0
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.requests = 0
        self.cache_hits = 0
        self.errors = 0
        self._last = 0.0
        self._album_details = {}
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as handle:
                    self.data = json.load(handle)
            except (ValueError, OSError):
                self.data = {}

    @staticmethod
    def _es_fallo_viejo(value):
        """Un fallo guardado por una version anterior hay que reintentarlo.

        Un None pelado es del formato antiguo, de cuando los fallos no llevaban
        marca: tambien caduca, o las mejoras de la busqueda no sirven de nada.
        """
        if value is None:
            return True
        return (isinstance(value, dict) and MISS in value
                and value[MISS] < SEARCH_VERSION)

    @staticmethod
    def key(artist, title):
        return "%s|%s" % (norm(artist), norm(title))

    def _put(self, artist, title, value):
        # Los fallos necesitan version. Guardar None otra vez haria que
        # _es_fallo_viejo lo considerase antiguo en cada ejecucion y repitiese
        # para siempre la misma consulta lenta.
        stored = {MISS: SEARCH_VERSION} if value is None else value
        self.data[self.key(artist, title)] = stored
        self.dirty += 1
        if self.dirty >= 25:
            self.flush()

    def flush(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False)
        tmp.replace(self.path)
        self.dirty = 0

    def close(self):
        self.flush()
        self.session.close()

    def _wait(self):
        elapsed = time.time() - self._last
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last = time.time()

    def _get(self, url):
        for attempt in range(self.max_retries):
            self._wait()
            try:
                self.requests += 1
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException:
                if attempt == self.max_retries - 1:
                    self.errors += 1
                    return None
                time.sleep(2 ** (attempt + 1))
                continue
            if not response.ok:
                if attempt == self.max_retries - 1:
                    self.errors += 1
                    return None
                time.sleep(2 ** (attempt + 1))
                continue
            try:
                payload = response.json()
            except ValueError:
                self.errors += 1
                return None
            # Deezer devuelve 200 con un objeto de error cuando se pasa el ritmo.
            if isinstance(payload, dict) and payload.get("error"):
                time.sleep(5)
                continue
            return payload
        return None

    def _album_detail(self, album_id):
        """Una misma edicion se consulta una vez aunque tenga muchas pistas."""
        if not album_id:
            return None
        if album_id not in self._album_details:
            self._album_details[album_id] = self._get(ALBUM % album_id)
        return self._album_details[album_id]

    @staticmethod
    def _verify(item, artist, title, duration=None):
        if not title_matches(item.get("title") or "", title):
            return False
        credit = ((item.get("artist") or {}).get("name")) or ""
        if not credit_matches(credit, artist):
            return False
        if duration:
            item_duration = float(item.get("duration") or 0)
            if (item_duration and abs(item_duration - duration)
                    > duration_tolerance(duration)):
                return False
        return True

    def lookup(self, artist, title, duration=None, want_date=True):
        """Devuelve album, fecha y URL de portada, o None."""
        if not title:
            return None

        cached = self.data.get(self.key(artist, title), "__missing__")
        if cached != "__missing__" and not self._es_fallo_viejo(cached):
            self.cache_hits += 1
            if isinstance(cached, dict) and MISS in cached:
                return None
            # La clave de cache no incluye la duracion, asi que hay que volver a
            # comprobarla: si no, una version corta se quedaria con el album de
            # la larga solo por haberse consultado antes.
            duration_mismatch = bool(
                cached and duration and cached.get("duration")
                and abs(cached["duration"] - duration)
                > duration_tolerance(duration))
            # Un resultado de otra version no demuestra que esta no exista. Se
            # repite la consulta para que las diez candidatas puedan filtrarse
            # con la duracion actual (p. ej. una version sinfonica frente a un
            # cover corto del mismo titulo).
            if duration_mismatch:
                cached = "__missing__"
            # ``portadas`` consulta con want_date=False para ahorrar una
            # peticion. Si despues ``scan`` pide los metadatos completos, esa
            # entrada parcial no debe dejar fecha y genero vacios para siempre.
            elif want_date and cached and not cached.get("_details_checked"):
                album_id = cached.get("album_id")
                if album_id:
                    detail = self._album_detail(album_id)
                    if detail:
                        cached = dict(cached)
                        cached["date"] = clean_date(
                            detail.get("release_date") or "")
                        cached["record_type"] = (
                            detail.get("record_type") or "").lower()
                        cached["genres"] = [
                            g.get("name") for g in
                            ((detail.get("genres") or {}).get("data") or [])
                            if g.get("name")]
                        cached["_details_checked"] = True
                        self._put(artist, title, cached)
                    return cached
                # Entrada de una version antigua que no guardaba album_id: se
                # repite la busqueda una sola vez y se actualiza el formato.
            elif not duration_mismatch:
                return cached

        if artist:
            query = 'artist:"%s" track:"%s"' % (artist, title)
        else:
            query = 'track:"%s"' % title
        url = "%s?q=%s&limit=10" % (SEARCH, urllib.parse.quote(query))
        payload = self._get(url)
        if payload is None:
            return None

        def verificados(respuesta):
            return [item for item in (respuesta.get("data") or [])
                    if self._verify(item, artist, title, duration)]

        candidates = verificados(payload)

        # Deezer es estricto con la busqueda por campos: si no encuentra nada
        # que valga, se reintenta en texto libre. No basta con mirar si vino
        # vacia: para CHEZ - Critical devolvia un unico tema ajeno ("Rolling 200
        # Deep"), y con eso ya no se probaba la busqueda que si lo encuentra.
        if not candidates:
            plain = urllib.parse.quote(("%s %s" % (artist, title)).strip())
            payload = self._get("%s?q=%s&limit=10" % (SEARCH, plain))
            if payload is None:
                return None
            candidates = verificados(payload)

        if not candidates:
            self._put(artist, title, None)
            return None

        best = min(candidates, key=lambda item: (_compilation_penalty(item),
                                                 -(item.get("rank") or 0)))
        album = best.get("album") or {}

        date, record_type, genres, details_checked = "", "", [], False
        if want_date and album.get("id"):
            detail = self._album_detail(album["id"])
            if detail:
                details_checked = True
                date = clean_date(detail.get("release_date") or "")
                record_type = (detail.get("record_type") or "").lower()
                # El genero viene en la misma respuesta que la fecha, asi que
                # sale gratis. Es la unica forma de dar genero a las carpetas
                # que son idioma y no genero: Aleman, Ruso, Frances, Ramdom.
                genres = [g.get("name") for g in
                          ((detail.get("genres") or {}).get("data") or [])
                          if g.get("name")]

        match = {
            "title": best.get("title") or "",
            "artist": ((best.get("artist") or {}).get("name")) or "",
            "album": album.get("title") or "",
            "date": date,
            "artwork_url": album.get("cover_xl") or album.get("cover_big") or "",
            "record_type": record_type,
            "genres": genres,
            "duration": float(best.get("duration") or 0),
            "album_id": album.get("id") or 0,
            "_details_checked": details_checked,
            "source": "deezer",
            "confidence": "revisar",
        }
        self._put(artist, title, match)
        return match

    def album_artwork(self, artist, album):
        """URL de la portada de un album concreto, sin pasar por la cancion.

        Hace falta cuando la fuente que identifico el tema dice de que disco es
        pero no trae imagen: Last.fm ya no sirve portadas, y MusicBrainz solo
        las tiene si alguien las subio. Con el nombre del disco, Deezer si.
        """
        if not album:
            return ""
        clave = "portada|%s|%s" % (norm(artist), norm(album))
        cached = self.data.get(clave, "__missing__")
        if cached != "__missing__":
            self.cache_hits += 1
            return cached or ""

        if artist:
            consulta = 'artist:"%s" album:"%s"' % (artist, album)
        else:
            consulta = 'album:"%s"' % album
        payload = self._get("%s/album?q=%s&limit=10"
                            % (SEARCH, urllib.parse.quote(consulta)))
        url = ""
        for item in ((payload or {}).get("data") or []):
            if not title_matches(item.get("title") or "", album):
                continue
            credit = ((item.get("artist") or {}).get("name")) or ""
            if artist and not credit_matches(credit, artist):
                continue
            url = item.get("cover_xl") or item.get("cover_big") or ""
            if url:
                break

        self.data[clave] = url
        self.dirty += 1
        if self.dirty >= 25:
            self.flush()
        return url

    # Sin otra cancion con que confirmarlo, solo se acepta un artista unico con
    # ese nombre exacto y con publico de verdad: con pocos seguidores es facil
    # que sea un homonimo.
    MIN_FANS_SIN_PRUEBA = 5000

    def artist_picture(self, name, titulos=()):
        """URL de la foto del artista, solo si es sin duda el mismo.

        Un nombre no basta: hay homonimos. Vale si alguna otra cancion de la
        biblioteca de ese artista esta entre sus temas en Deezer, con el titulo
        exacto. Deezer da una imagen generica a quien no tiene foto
        (".../artist//..."): esa no se usa nunca.
        """
        from .text import bare_title

        if not name:
            return ""
        clave = "foto|%s" % norm(name)
        cached = self.data.get(clave, "__missing__")
        if cached != "__missing__":
            self.cache_hits += 1
            return cached or ""

        payload = self._get("%s/artist?q=%s&limit=10"
                            % (SEARCH, urllib.parse.quote(name)))
        if payload is None:
            return ""
        exactos = [item for item in (payload.get("data") or [])
                   if norm(item.get("name") or "") == norm(name)]
        propios = {norm(bare_title(t)) for t in titulos if t}
        propios.discard("")
        url = ""
        for artista in exactos[:3]:
            foto = artista.get("picture_xl") or ""
            if not foto or "/artist//" in foto:
                continue
            if propios:
                top = self._get("https://api.deezer.com/artist/%s/top?limit=50"
                                % artista.get("id"))
                temas = {norm(bare_title(t.get("title") or ""))
                         for t in ((top or {}).get("data") or [])}
                if temas & propios:
                    url = foto
                    break
            elif (len(exactos) == 1
                  and (artista.get("nb_fan") or 0) >= self.MIN_FANS_SIN_PRUEBA):
                url = foto
        self.data[clave] = url
        self.dirty += 1
        if self.dirty >= 25:
            self.flush()
        return url

    def fetch_artwork(self, url):
        if not url:
            return None
        for attempt in range(self.max_retries):
            self._wait()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException:
                if attempt == self.max_retries - 1:
                    self.errors += 1
                    return None
                time.sleep(2 ** (attempt + 1))
                continue
            if response.ok and response.content:
                return response.content
            return None
        return None

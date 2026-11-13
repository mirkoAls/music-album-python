"""Busqueda en iTunes, sin clave.

Acierta un 67% sobre la muestra de 40 archivos, igual que Deezer y por debajo
del 77% de MusicBrainz. No aporto ningun resultado exclusivo. Se mantiene en la
cadena porque en modo consenso un tercer voto independiente vale, y porque
devuelve la portada en la misma llamada.
"""

import json
import time
import urllib.parse
from pathlib import Path

import requests

from .text import clean_date, credit_matches, norm, title_matches

ENDPOINT = "https://itunes.apple.com/search"

# Apple recomienda no pasar de unas 20 llamadas por minuto.
MIN_INTERVAL = 3.1

# artworkUrl100 trae una miniatura de 100 px; la URL admite otros tamanos, y si
# el original es menor devuelve el original sin inventar pixeles. Ver
# covers.LADO_PEDIDO.
ARTWORK_SIZE = "1500x1500"


# Version de la logica de busqueda. Los fallos ("no existe") se guardan con esta
# marca, y al subirla se reintentan todos: si no, una mejora en la busqueda no
# sirve de nada porque la cache sigue afirmando que la cancion no existe.
SEARCH_VERSION = 4

MISS = "_miss"

class ItunesClient:
    def __init__(self, cache_path, timeout=20, max_retries=3, country="US"):
        self.path = Path(cache_path)
        self.data = {}
        self.dirty = 0
        self.timeout = timeout
        self.max_retries = max_retries
        self.country = country
        self.session = requests.Session()
        self.requests = 0
        self.cache_hits = 0
        self.errors = 0
        self._last = 0.0
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as handle:
                    self.data = json.load(handle)
            except (ValueError, OSError):
                self.data = {}

    # ---------------------------------------------------------------- cache
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

    # ---------------------------------------------------------------- red
    def _wait(self):
        elapsed = time.time() - self._last
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last = time.time()

    def _get(self, term, entity="song"):
        params = {
            "term": term,
            "media": "music",
            "entity": entity,
            "limit": 25,
            "country": self.country,
        }
        url = "%s?%s" % (ENDPOINT, urllib.parse.urlencode(params))
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
            if response.status_code == 403:
                # Apple corta cuando se le pide demasiado seguido.
                time.sleep(10)
                continue
            if not response.ok:
                if attempt == self.max_retries - 1:
                    self.errors += 1
                    return None
                time.sleep(2 ** (attempt + 1))
                continue
            try:
                return response.json()
            except ValueError:
                self.errors += 1
                return None
        return None

    # ------------------------------------------------------------ eleccion
    @staticmethod
    def _verify(item, artist, title):
        if not title_matches(item.get("trackName") or "", title):
            return False
        return credit_matches(item.get("artistName") or "", artist)

    @staticmethod
    def _penalty(item):
        """Prefiere el album de estudio sobre el directo o el recopilatorio."""
        collection = norm(item.get("collectionName") or "")
        penalty = 0
        for word, cost in (("live", 4), ("en vivo", 4), ("en directo", 4),
                           ("greatest hits", 3), ("grandes exitos", 3),
                           ("remix", 2), ("karaoke", 6), ("tribute", 6),
                           ("cover", 4), ("instrumental", 3)):
            if word in collection:
                penalty += cost
        if item.get("collectionName") and item.get("trackCount", 0) == 1:
            penalty += 1
        return penalty

    def lookup(self, artist, title):
        """Devuelve album, fecha y URL de portada, o None."""
        if not title:
            return None

        cached = self.data.get(self.key(artist, title), "__missing__")
        if cached != "__missing__" and not self._es_fallo_viejo(cached):
            self.cache_hits += 1
            if isinstance(cached, dict) and MISS in cached:
                return None
            return cached

        term = ("%s %s" % (artist, title)).strip()
        payload = self._get(term)
        if payload is None:
            return None

        ranked = []
        for item in payload.get("results", []):
            if not self._verify(item, artist, title):
                continue
            ranked.append((self._penalty(item),
                           item.get("releaseDate") or "9999", item))

        if not ranked:
            self._put(artist, title, None)
            return None

        ranked.sort(key=lambda row: (row[0], row[1]))
        best = ranked[0][2]

        artwork = best.get("artworkUrl100") or ""
        if artwork:
            artwork = artwork.replace("100x100", ARTWORK_SIZE)

        match = {
            "title": best.get("trackName") or "",
            "artist": best.get("artistName") or "",
            "album": best.get("collectionName") or "",
            "date": clean_date(best.get("releaseDate") or ""),
            "artwork_url": artwork,
            # iTunes trae el genero en cada resultado, sin llamada extra.
            "genres": [g for g in [best.get("primaryGenreName")] if g],
            "source": "itunes",
            "confidence": "revisar",
        }
        self._put(artist, title, match)
        return match

    def album_artwork(self, artist, album):
        """Portada de un album concreto. Segundo intento tras Deezer."""
        if not album:
            return ""
        clave = "portada|%s|%s" % (norm(artist), norm(album))
        cached = self.data.get(clave, "__missing__")
        if cached != "__missing__":
            self.cache_hits += 1
            return cached or ""

        payload = self._get(("%s %s" % (artist, album)).strip(), entity="album")
        url = ""
        for item in ((payload or {}).get("results") or []):
            if not title_matches(item.get("collectionName") or "", album):
                continue
            if artist and not credit_matches(item.get("artistName") or "", artist):
                continue
            bruto = item.get("artworkUrl100") or ""
            if bruto:
                url = bruto.replace("100x100", ARTWORK_SIZE)
                break

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

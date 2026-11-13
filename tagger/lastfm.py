"""Busqueda en Last.fm. Cuarta fuente, opcional.

Last.fm se nutre de lo que escucha la gente, no de un catalogo comercial, asi
que conoce temas que Deezer, iTunes y MusicBrainz no tienen: mixtapes, ediciones
regionales, rap aleman de nicho. Devuelve album, portada y etiquetas de genero.

Necesita una clave gratuita. Se saca en dos minutos en:

    https://www.last.fm/api/account/create

Pon la clave en la variable de entorno LASTFM_API_KEY. Sin clave, la fuente se desactiva sola y
el resto sigue funcionando igual.
"""

import json
import os
import time
import urllib.parse
from pathlib import Path

import requests

from .text import credit_matches, norm, title_matches

ENDPOINT = "https://ws.audioscrobbler.com/2.0/"

# Clave de https://www.last.fm/api/account/create (app "Musik Founder").
# El "shared secret" no hace falta: solo sirve para escribir en la cuenta,
# y aqui solo se lee.
API_KEY = os.environ.get("LASTFM_API_KEY", "")

# Last.fm no publica un limite duro; se recomienda no pasar de 5 por segundo.
MIN_INTERVAL = 0.25

# Tamanos de imagen que devuelve, de menor a mayor.
IMAGE_ORDER = ["mega", "extralarge", "large", "medium", "small"]


def key_is_configured():
    return bool(API_KEY and API_KEY.strip())


# Version de la logica de busqueda. Los fallos ("no existe") se guardan con esta
# marca, y al subirla se reintentan todos: si no, una mejora en la busqueda no
# sirve de nada porque la cache sigue afirmando que la cancion no existe.
SEARCH_VERSION = 4

MISS = "_miss"

class LastfmClient:
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

    def _call(self, method, **params):
        params.update({"method": method, "api_key": API_KEY, "format": "json"})
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
            if isinstance(payload, dict) and payload.get("error"):
                # 6 = no encontrado; el resto son problemas de ritmo o de clave.
                if payload.get("error") == 6:
                    return {}
                time.sleep(3)
                continue
            return payload
        return None

    @staticmethod
    def _best_image(images):
        """La imagen mas grande que traiga, o cadena vacia."""
        por_tamano = {}
        for item in images or []:
            size = (item.get("size") or "").lower()
            url = item.get("#text") or ""
            if url:
                por_tamano[size] = url
        for size in IMAGE_ORDER:
            if por_tamano.get(size):
                return por_tamano[size]
        return ""

    def lookup(self, artist, title, duration=None, want_date=True):
        """Devuelve album, portada y generos, o None. La fecha no la da."""
        if not title or not key_is_configured():
            return None

        cached = self.data.get(self.key(artist, title), "__missing__")
        if cached != "__missing__" and not self._es_fallo_viejo(cached):
            self.cache_hits += 1
            if isinstance(cached, dict) and MISS in cached:
                return None
            return cached

        payload = self._call("track.getInfo", artist=artist or "", track=title,
                             autocorrect=1)
        if payload is None:
            return None

        track = (payload or {}).get("track") or {}
        if not track:
            self._put(artist, title, None)
            return None

        cand_title = track.get("name") or ""
        cand_artist = ((track.get("artist") or {}).get("name")) or ""
        if not title_matches(cand_title, title):
            self._put(artist, title, None)
            return None
        if not credit_matches(cand_artist, artist):
            self._put(artist, title, None)
            return None

        album = track.get("album") or {}
        generos = [t.get("name") for t in
                   ((track.get("toptags") or {}).get("tag") or [])
                   if t.get("name")]

        match = {
            "title": cand_title,
            "artist": cand_artist,
            "album": album.get("title") or "",
            # Last.fm no devuelve fecha de publicacion en track.getInfo.
            "date": "",
            "artwork_url": self._best_image(album.get("image")),
            "genres": generos[:3],
            "source": "lastfm",
            "confidence": "revisar",
        }
        self._put(artist, title, match)
        return match

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

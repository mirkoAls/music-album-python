"""Cliente de MusicBrainz: limite de peticiones, cache, umbral de confianza."""

import json
import os
import re
import time
from pathlib import Path

import musicbrainzngs

from .text import clean_date, credit_matches, norm, title_matches

APP_NAME = "MusicLibraryTagger"
APP_VERSION = "1.0"

# MusicBrainz y Cover Art Archive exigen un contacto real en el User-Agent y
# bloquean a quien no lo manda.
CONTACT = os.environ.get("MUSICBRAINZ_CONTACT", "PON-AQUI-TU-CORREO@example.com")

RETRYABLE = (
    musicbrainzngs.NetworkError,
    musicbrainzngs.ResponseError,
)


def contact_is_configured():
    return "PON-AQUI" not in CONTACT and "@" in CONTACT


# Version de la logica de busqueda. Los fallos ("no existe") se guardan con esta
# marca, y al subirla se reintentan todos: si no, una mejora en la busqueda no
# sirve de nada porque la cache sigue afirmando que la cancion no existe.
SEARCH_VERSION = 4

MISS = "_miss"

class Cache:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.dirty = 0
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as handle:
                    self.data = json.load(handle)
            except (ValueError, OSError):
                self.data = {}

    @staticmethod
    def key(artist, title):
        return "%s|%s" % (norm(artist), norm(title))

    def get(self, artist, title):
        value = self.data.get(self.key(artist, title), "__missing__")
        if isinstance(value, dict) and MISS in value:
            # Fallo de una version anterior de la busqueda: se reintenta.
            return "__missing__" if value[MISS] < SEARCH_VERSION else None
        if value is None:
            # Formato antiguo, sin marca de version: se reintenta.
            return "__missing__"
        return value

    def put(self, artist, title, value):
        self.data[self.key(artist, title)] = value
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


def _credit_to_name(artist_credit):
    names = []
    for item in artist_credit or []:
        if isinstance(item, dict):
            names.append(item.get("artist", {}).get("name", ""))
        elif isinstance(item, str):
            names.append(item)
    joined = "".join(n for n in names if n)
    return joined.strip()


# Penalizacion por tipo de edicion. search_recordings devuelve una sola edicion
# por grabacion, asi que la eleccion real se hace entre grabaciones candidatas:
# sin esto, "Titi me pregunto" acababa en la emision en directo en vez de en
# "Un Verano Sin Ti".
PRIMARY_PENALTY = {
    "album": 0,
    "single": 1,
    "ep": 2,
    "compilation": 5,
    "broadcast": 6,
    "other": 6,
}

SECONDARY_PENALTY = {
    "live": 4,
    "compilation": 4,
    "remix": 3,
    "dj-mix": 3,
    "mixtape/street": 2,
    "demo": 2,
    "soundtrack": 3,
}

LIVE_TITLE_RE = re.compile(r"\((?:live|en vivo|en directo)\)|\ben vivo\b|\ben directo\b",
                           re.IGNORECASE)


def _release_penalty(release):
    if not release:
        return 20
    group = release.get("release-group", {}) or {}
    primary = (group.get("primary-type") or "").lower()
    penalty = PRIMARY_PENALTY.get(primary, 4)
    for secondary in (group.get("secondary-type-list") or []):
        penalty += SECONDARY_PENALTY.get(secondary.lower(), 1)
    if LIVE_TITLE_RE.search(release.get("title") or ""):
        penalty += 3
    if (release.get("status") or "").lower() not in ("official", ""):
        penalty += 2
    # Una edicion sin fecha suele estar mal documentada: bootlegs, recopilatorios
    # sueltos, ediciones fantasma.
    if not (release.get("date") or "").strip():
        penalty += 2
    return penalty


def _pick_release(releases):
    """Mejor edicion dentro de una misma grabacion."""
    if not releases:
        return None
    return sorted(releases, key=lambda r: (_release_penalty(r), r.get("date") or "9999"))[0]


class MusicBrainzClient:
    def __init__(self, cache_path, min_score=90, review_score=78, limit=40,
                 max_retries=3):
        musicbrainzngs.set_useragent(APP_NAME, APP_VERSION, CONTACT)
        musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
        self.cache = Cache(cache_path)
        self.min_score = min_score
        self.review_score = review_score
        self.limit = limit
        self.max_retries = max_retries
        self.requests = 0
        self.cache_hits = 0
        self.errors = 0

    def _search(self, artist, title):
        for attempt in range(self.max_retries):
            try:
                self.requests += 1
                return musicbrainzngs.search_recordings(
                    artist=artist, recording=title, limit=self.limit
                )
            except RETRYABLE as error:
                if attempt == self.max_retries - 1:
                    self.errors += 1
                    raise
                time.sleep(2 ** (attempt + 1))
        return None

    def _verify(self, candidate, artist, title):
        """Contraste local: el score de MusicBrainz solo es generoso a veces."""
        if not title_matches(candidate.get("title", ""), title):
            return False
        return credit_matches(_credit_to_name(candidate.get("artist-credit")), artist)

    def lookup(self, artist, title):
        """Devuelve dict con la coincidencia, o None.

        confidence: "alta" si supera min_score y verifica localmente;
        "revisar" si esta entre review_score y min_score.
        """
        if not title:
            return None

        cached = self.cache.get(artist, title)
        if cached != "__missing__":
            self.cache_hits += 1
            return cached

        try:
            result = self._search(artist, title)
        except RETRYABLE:
            return None

        ranked = []
        for candidate in (result or {}).get("recording-list", []):
            try:
                score = int(candidate.get("ext:score", 0))
            except (TypeError, ValueError):
                score = 0
            if score < self.review_score:
                continue
            if not self._verify(candidate, artist, title):
                continue
            release = _pick_release(candidate.get("release-list"))
            # Primero el tipo de edicion, luego el score y por ultimo la fecha:
            # entre dos grabaciones que son la misma cancion interesa la del
            # album de estudio, y de ese album la edicion mas antigua.
            # MusicBrainz trata cada actuacion en directo como una grabacion
            # distinta, asi que hace falta un limit alto para que la de estudio
            # llegue a estar entre las candidatas.
            ranked.append((_release_penalty(release), -score,
                           (release or {}).get("date") or "9999", candidate, release))

        if not ranked:
            self.cache.put(artist, title, {MISS: SEARCH_VERSION})
            return None

        ranked.sort(key=lambda item: item[:3])
        _, negative_score, _, recording, release = ranked[0]
        score = -negative_score
        group = (release or {}).get("release-group", {}) or {}
        match = {
            "title": recording.get("title", ""),
            "artist": _credit_to_name(recording.get("artist-credit")),
            "album": (release or {}).get("title", ""),
            "date": clean_date((release or {}).get("date", "")),
            "score": score,
            "recording_id": recording.get("id", ""),
            "release_id": (release or {}).get("id", ""),
            "release_group_id": group.get("id", ""),
            "confidence": "alta" if score >= self.min_score else "revisar",
        }
        self.cache.put(artist, title, match)
        return match

    def close(self):
        self.cache.flush()

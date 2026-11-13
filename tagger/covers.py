"""Descarga de portadas desde Cover Art Archive.

Cover Art Archive es gratuito y no pide clave, pero se direcciona por el ID de
edicion de MusicBrainz: hay que resolver antes la cancion contra MusicBrainz.
"""

import hashlib
import io
import json
import os
import re
import struct
import time
from collections import OrderedDict
from pathlib import Path

import requests

from .musicbrainz import APP_NAME, APP_VERSION, CONTACT

BASE = "https://coverartarchive.org"

# Portadas rotas o insignificantes: en esta biblioteca hay APIC de 0 bytes.
MIN_USEFUL_BYTES = 1024

# Cover Art Archive redirige a archive.org. Sin limite estricto, pero se va
# despacio por cortesia.
MIN_INTERVAL = 0.5

# Tamano al que se piden las portadas. Las tres fuentes reducen, nunca
# amplian: si el original es menor devuelven el original. Medido sobre la
# cache: Deezer a 1500 da 1500 o 1200, iTunes 1500 o menos, y Cover Art Archive
# a 1200 da 1200 o el original (y nunca falla donde a 500 no fallaba). Antes se
# pedian a 1000, 600 y 500, y por eso tantas acababan "poco HD".
LADO_PEDIDO = 1500
LADO_CAA = 1200

# Una portada de este lado ya es buena. Por debajo se mejora si la fuente trae
# la misma foto en grande; por encima no se reescribe el archivo solo por ganar
# pixeles, porque cada reescritura rompe la sincronizacion con el movil.
LADO_HD = 1000

_TAMANO_EN_URL = (
    # Deezer: ".../images/cover/<hash>/1000x1000-000000-80-0-0.jpg"
    re.compile(r"dzcdn\.net/images/(?:cover|artist)/[0-9a-f]+/(\d{2,4})x(\d{2,4})-"),
    # iTunes: ".../image/thumb/.../600x600bb.jpg"
    re.compile(r"mzstatic\.com/image/thumb/.+/(\d{2,4})x(\d{2,4})bb\.(?:jpg|png)$"),
)


def url_en_grande(url):
    """La misma portada pedida a LADO_PEDIDO, si la URL la pide mas pequena.

    Las URL que ya estan en la cache son de cuando se pedian a 1000 o a 600;
    se agrandan al descargar, sin volver a preguntar a la fuente.
    """
    for patron in _TAMANO_EN_URL:
        encontrado = patron.search(url or "")
        if encontrado and int(encontrado.group(1)) < LADO_PEDIDO:
            return "%s%dx%d%s" % (url[:encontrado.start(1)], LADO_PEDIDO,
                                  LADO_PEDIDO, url[encontrado.end(2):])
    return url


class CoverCache:
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

    def get(self, key):
        return self.data.get(key, "__missing__")

    def put(self, key, value):
        self.data[key] = value
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


class CoverArtClient:
    def __init__(self, cache_path, size=500, timeout=20, max_retries=3):
        self.cache = CoverCache(cache_path)
        self.size = size
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "%s/%s ( %s )" % (
            APP_NAME, APP_VERSION, CONTACT)
        self.downloads = 0
        self.resolved = 0
        self.misses = 0
        self.cache_hits = 0
        self.errors = 0
        self._last_request = 0.0

    def _wait(self):
        elapsed = time.time() - self._last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_request = time.time()

    def _get(self, url):
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

            if response.status_code == 404:
                return None
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == self.max_retries - 1:
                    self.errors += 1
                    return None
                time.sleep(2 ** (attempt + 1))
                continue
            if response.ok and response.content:
                return response.content
            return None
        return None

    def _a_mi_tamano(self, url):
        """Las URL de la cache son de cuando se pedia front-500."""
        encontrado = re.search(r"/front-(\d+)$", url)
        if encontrado and int(encontrado.group(1)) < self.size:
            return "%s%d" % (url[:encontrado.start(1)], self.size)
        return url

    def fetch(self, release_id, release_group_id=""):
        """Devuelve los bytes de la portada, o None si no hay."""
        key = release_id or release_group_id
        if not key:
            return None

        # La cache guarda la URL resuelta, no los bytes: la imagen se vuelve a
        # pedir. En el uso normal no se nota, porque los archivos que ya tienen
        # portada ni siquiera llegan hasta aqui.
        cached = self.cache.get(key)
        if cached != "__missing__":
            self.cache_hits += 1
            if cached == "none" or not cached:
                return None
            data = self._get(self._a_mi_tamano(cached))
            if data:
                self.downloads += 1
            return data

        candidates = []
        if release_id:
            candidates.append("%s/release/%s/front-%d" % (BASE, release_id, self.size))
        if release_group_id:
            candidates.append("%s/release-group/%s/front-%d"
                              % (BASE, release_group_id, self.size))

        for url in candidates:
            data = self._get(url)
            if data and len(data) >= MIN_USEFUL_BYTES:
                self.cache.put(key, url)
                self.downloads += 1
                self.resolved += 1
                return data

        self.cache.put(key, "none")
        self.misses += 1
        return None

    def close(self):
        self.cache.flush()
        self.session.close()


# Una caratula de album es cuadrada. Las miniaturas de video son 16:9, y las
# imagenes recortadas o diminutas tampoco valen como portada.
MIN_SIDE = 250
SQUARE_TOLERANCE = 0.12
LETTERBOX_MIN_FRACTION = 0.12


def image_size(data):
    """Ancho y alto de un JPEG o PNG, sin dependencias externas."""
    if not data or len(data) < 24:
        return None, None
    if data[:8].startswith(b"\x89PNG"):
        try:
            return struct.unpack(">II", data[16:24])
        except struct.error:
            return None, None
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            try:
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return width, height
            except struct.error:
                return None, None
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        try:
            length = struct.unpack(">H", data[index + 2:index + 4])[0]
        except struct.error:
            return None, None
        index += 2 + length
    return None, None


def _flat_edge_band(image, horizontal, reverse=False):
    """Filas/columnas planas y del mismo color contiguas al borde."""
    from PIL import ImageStat

    width, height = image.size
    length = height if horizontal else width
    indexes = range(length - 1, -1, -1) if reverse else range(length)
    reference = None
    run = 0
    for index in indexes:
        box = ((0, index, width, index + 1) if horizontal
               else (index, 0, index + 1, height))
        stats = ImageStat.Stat(image.crop(box))
        ranges = [high - low for low, high in stats.extrema[:3]]
        color = stats.mean[:3]
        if max(ranges) > 18:
            break
        if reference is None:
            reference = color
        elif max(abs(a - b) for a, b in zip(color, reference)) > 18:
            break
        run += 1
        if run >= length // 3:
            break
    return run, reference


def has_video_bars(data):
    """Detecta una miniatura 16:9 centrada dentro de un lienzo cuadrado.

    Las descargas de YouTube de esta biblioteca suelen traer PNG de 500x500,
    pero la imagen real ocupa solo la franja central y deja dos bandas blancas
    o negras. Mirar solo el ancho y alto exterior las hacia pasar por caratulas.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
            image.thumbnail((96, 96))
    except (ImportError, OSError, ValueError):
        return False

    width, height = image.size
    for horizontal, length in ((True, height), (False, width)):
        first, first_color = _flat_edge_band(image, horizontal)
        last, last_color = _flat_edge_band(image, horizontal, reverse=True)
        minimum = max(2, int(round(length * LETTERBOX_MIN_FRACTION)))
        same_color = (first_color is not None and last_color is not None
                      and max(abs(a - b) for a, b in
                              zip(first_color, last_color)) <= 24)
        if first >= minimum and last >= minimum and same_color:
            return True
    return False


# Analizar una portada cuesta unos 5 ms (hay que abrirla y medir las bandas), y
# en una pasada completa la misma imagen se examina tres veces: al elegir que
# archivos tocar, al decidir si se sustituye y al repartir portadas entre temas
# del mismo disco. Se guarda el veredicto por contenido.
_VERDICTOS = OrderedDict()
_VERDICTOS_MAX = 8192


def bad_cover(data):
    """La portada existe pero no sirve: motivo, o cadena vacia si esta bien."""
    if not data or len(data) < MIN_USEFUL_BYTES:
        return "vacia o rota"

    clave = (len(data), hashlib.sha1(data).digest())
    if clave in _VERDICTOS:
        _VERDICTOS.move_to_end(clave)
        return _VERDICTOS[clave]
    veredicto = _bad_cover_uncached(data)
    _VERDICTOS[clave] = veredicto
    while len(_VERDICTOS) > _VERDICTOS_MAX:
        _VERDICTOS.popitem(last=False)
    return veredicto


def _bad_cover_uncached(data):
    width, height = image_size(data)
    if not width or not height:
        return ""
    if min(width, height) < MIN_SIDE:
        return "diminuta (%dx%d)" % (width, height)
    ratio = width / float(height)
    if abs(ratio - 1.0) > SQUARE_TOLERANCE:
        return "no es cuadrada (%dx%d)" % (width, height)
    if has_video_bars(data):
        return "tiene bandas de miniatura de video"
    return ""


def picture_signature(data):
    """Huella visual de la imagen, para saber si dos son la MISMA foto.

    Es un dHash: se reduce a 9x8 en gris y se guarda, por cada pixel, si es mas
    claro que el de su derecha. Sobrevive al recorte de calidad, al reescalado y
    al cambio de formato, que es justo lo que distingue "la misma portada
    guardada de otra manera" de "la portada de otro disco".
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as origen:
            imagen = origen.convert("L").resize((9, 8), Image.BILINEAR)
    except (ImportError, OSError, ValueError):
        return None

    pixeles = list(imagen.getdata())
    bits = 0
    for fila in range(8):
        base = fila * 9
        for columna in range(8):
            bits <<= 1
            if pixeles[base + columna] > pixeles[base + columna + 1]:
                bits |= 1
    return bits


# Cuantos bits pueden diferir y seguir siendo la misma foto. Con 64 bits, cinco
# de margen aguanta recompresiones agresivas sin confundir dos portadas.
PICTURE_TOLERANCE = 5


def same_picture(a, b, tolerance=PICTURE_TOLERANCE):
    """Son la misma imagen, aunque cambien tamano, formato o compresion?"""
    if not a or not b:
        return False
    if a == b:
        return True
    fa, fb = picture_signature(a), picture_signature(b)
    if fa is None or fb is None:
        return False
    return bin(fa ^ fb).count("1") <= tolerance


def min_side(data):
    """Lado menor de la imagen, o 0 si no se sabe.

    image_size devuelve (None, None) para todo lo que no sea JPEG ni PNG (un
    WebP, una imagen rota). Hacer min() sobre eso rompia el programa, asi que
    aqui se traduce a 0: tamano desconocido no gana ninguna comparacion.
    """
    ancho, alto = image_size(data) if data else (None, None)
    if not ancho or not alto:
        return 0
    return min(ancho, alto)


def size_text(data):
    """Ancho x alto para los informes, o "?" si la imagen no se puede leer."""
    ancho, alto = image_size(data) if data else (None, None)
    return "%dx%d" % (ancho, alto) if ancho and alto else "?"


# Veredictos de decide_cover. Los tres primeros escriben; los dos ultimos no.
COVER_PUT = "poner"          # no habia, o la que habia no servia
COVER_UPGRADE = "mejorar"    # la misma foto con mas pixeles
COVER_FIX = "corregir"       # otra imagen, y la que habia no era de fiar
COVER_KEEP = "conservar"     # se deja la que hay, y se anota para revisar
COVER_SAME = "identica"      # se deja la que hay, y no hay nada que revisar
COVER_NONE = "sin candidata"  # la fuente no trajo imagen


def decide_cover(current, candidate, from_program=False, shared=False):
    """Que hacer con la portada de un archivo. Una sola regla, un solo sitio.

    current      bytes de la portada que tiene ahora, o vacio
    candidate    bytes de la que proponen las fuentes, o vacio
    from_program esta portada la escribio este programa (esta en el registro)
    shared       la misma imagen esta repartida entre discos distintos, o sea
                 que es resto de un reparto y no la caratula de este tema

    La idea: conservar lo que esta bien y corregir lo que esta mal son cosas
    distintas, y lo que las separa no es el aspecto de la imagen sino de donde
    salio. Una caratula cuadrada y grande puede ser perfectamente la de otra
    cancion.
    """
    if not candidate:
        return COVER_NONE
    if current == candidate:
        return COVER_SAME
    if not current or bad_cover(current):
        return COVER_PUT
    if same_picture(current, candidate):
        # La misma foto solo se cambia si la que hay se queda corta. Una de
        # 1000 px ya es buena: reescribir el archivo por unos pixeles mas no se
        # nota en la pantalla y si rompe la sincronizacion con el movil.
        if min_side(current) < LADO_HD and min_side(candidate) > min_side(current):
            return COVER_UPGRADE
        return COVER_SAME
    if from_program or shared:
        # Es responsabilidad del programa y la puede corregir. Entre dos
        # imagenes distintas gana la de mas resolucion, salvo que la que hay
        # este repartida: esa no es de esta cancion por mucho que mida mas.
        if min_side(candidate) < min_side(current) and not shared:
            return COVER_KEEP
        return COVER_FIX
    # No la puso el programa: la traia el video o la puso el usuario. Aqui no
    # hay con que decidir, asi que no se decide.
    return COVER_KEEP


def needs_cover(info):
    return info is not None and info.get("cover_bytes", 0) < MIN_USEFUL_BYTES


# Portadas puestas a mano por el usuario: no caducan nunca, ni al subir la
# version. Las eligio una persona mirando la imagen; ninguna heuristica gana a
# eso.
MANUAL_SOURCE = "elegida a mano"

# Foto del artista, puesta cuando no habia caratula ni de la cancion ni del
# disco. Es un apano: cada pasada vuelve a buscar la de verdad, y en cuanto una
# fuente la traiga la sustituye aunque sea mas pequena.
ARTIST_PHOTO_SOURCE = "foto del artista"

LEDGER_VERSION = 3


class CoverLedger:
    """Recuerda que portadas puso el programa y de que fuente salieron.

    Sin esto, revisar la biblioteca entera vuelve a descargar cada imagen en
    cada pasada: son miles de archivos y cientos de megas por nada. Solo se dan
    por hechas las portadas que puso el propio programa desde una fuente
    confirmada y que siguen intactas en el archivo. Cualquier otra (la que traia
    el video, la heredada de otro tema, o una que hayas cambiado tu) se vuelve a
    comprobar.

    Al guardar no se vuelca entero lo que hay en memoria: solo lo que cambio en
    esta ejecucion, encima de lo que haya en disco. Si otro proceso apunto
    portadas mientras tanto se conservan; pisarlas es como se perdieron 39
    elegidas a mano.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.data = self._leer() or {}
        self.dirty = 0
        self._cambios = {}

    def _leer(self):
        """Lo que hay en disco, o None si existe pero no se puede leer."""
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (ValueError, OSError):
            return None

    @staticmethod
    def _stamp(path):
        """Fecha y tamano. Windows no siempre refresca la fecha cuando dos
        escrituras caen muy seguidas, y el tamano lo delata igual."""
        try:
            estado = path.stat()
        except (OSError, ValueError):
            return None
        return [estado.st_mtime_ns, estado.st_size]

    def already_done(self, path, cover_data):
        """Esta portada la puso el programa y nadie la ha tocado desde entonces?"""
        registro = self.data.get(str(path))
        if not registro or not cover_data:
            return False
        if registro.get("fuente") == MANUAL_SOURCE:
            return list(registro.get("mtime") or []) == self._stamp(path) and \
                registro.get("sha1") == hashlib.sha1(cover_data).hexdigest()
        # Toda entrada de una version anterior se vuelve a comprobar, hayan
        # coincidido dos fuentes o una. Antes se perdonaba a las de varias
        # fuentes, y era justo el caso peligroso: "Veridis Quo" tenia dos
        # fuentes de acuerdo en que el disco era Discovery, y aun asi acabo con
        # la caratula del single "Harder Better Faster Stronger", porque la
        # imagen se cogia de cualquier respuesta y no de la del disco ganador.
        # Eso ya esta arreglado, pero las portadas que dejo siguen puestas.
        if registro.get("version", 1) < LEDGER_VERSION:
            return False
        if list(registro.get("mtime") or []) != self._stamp(path):
            return False
        return registro.get("sha1") == hashlib.sha1(cover_data).hexdigest()

    def placed_by_program(self, path):
        """La portada que hay ahora la puso este programa?

        Si esta apuntada, salio de aqui, y entonces se puede corregir: las
        equivocadas que hay en la biblioteca las escribio una version anterior.
        Las elegidas a mano no cuentan: esas mandan siempre.
        """
        registro = self.data.get(str(path))
        if not registro:
            return False
        return registro.get("fuente") != MANUAL_SOURCE

    def fuente(self, path):
        """De donde salio la portada apuntada para este archivo, o ""."""
        return (self.data.get(str(path)) or {}).get("fuente", "") or ""

    def record(self, path, cover_data, source=""):
        marca = self._stamp(path)
        if marca is None or not cover_data:
            return
        entrada = {
            "mtime": marca,
            "sha1": hashlib.sha1(cover_data).hexdigest(),
            "fuente": source,
            "version": LEDGER_VERSION,
        }
        self.data[str(path)] = entrada
        self._anotar(str(path), entrada)

    def move(self, origen, destino):
        """El archivo se renombro: su registro, y con el su proteccion, va con
        el. Antes se quedaba en la ruta vieja y la portada elegida a mano
        pasaba a ser una cualquiera."""
        entrada = self.data.pop(str(origen), None)
        if entrada is None:
            return
        self.data[str(destino)] = entrada
        self._anotar(str(origen), None)
        self._anotar(str(destino), entrada)

    def _anotar(self, clave, entrada):
        self._cambios[clave] = entrada
        self.dirty += 1
        if self.dirty >= 25:
            self.flush()

    def flush(self):
        if not self._cambios:
            return
        # Si el archivo existe pero no se puede leer no se rehace desde cero:
        # se parte de lo que hay en memoria, que es la copia completa.
        base = self._leer()
        if base is None:
            base = dict(self.data)
        for clave, entrada in self._cambios.items():
            if entrada is None:
                base.pop(clave, None)
            else:
                base[clave] = entrada
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("%s.%d.tmp" % (self.path.name, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(base, handle, ensure_ascii=False)
        tmp.replace(self.path)
        self.data = base
        self._cambios = {}
        self.dirty = 0


def guess_mime(data):
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    return "image/jpeg"

"""Lectura y escritura de tags, con copia de seguridad reversible."""

import json
import signal
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen import MutagenError

FIELDS = ("title", "artist", "album", "albumartist", "genre", "date", "tracknumber")

SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav"}


def scan_library(root, extensions=SUPPORTED_EXTENSIONS):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


# Cache de lecturas. En una pasada completa el mismo archivo se abre muchas
# veces: el paso de artistas, el analisis, la identidad, el backup y el reparto
# de portadas. La clave lleva la fecha y el tamano del archivo, asi que en
# cuanto se escribe algo la entrada caduca sola y nunca se sirve un dato viejo.
_TAG_CACHE = {}
_TAG_CACHE_MAX = 40000

# Duracion, bitrate y tamano pesan muy poco. Se conservan aparte de las
# portadas para toda la biblioteca: ``scan`` ya abre el archivo al leer tags y
# no debe abrirlo otra vez solo para obtener su duracion.
_BASIC_INFO_CACHE = {}
_BASIC_INFO_CACHE_MAX = 40000

# Las portadas ocupan cientos de kilobytes, asi que de estas se guardan pocas y
# se tira la mas antigua.
_INFO_CACHE = OrderedDict()
_INFO_CACHE_MAX = 64


def _stamp(path):
    """Identidad de la version actual del archivo: ruta, fecha y tamano."""
    try:
        status = path.stat()
    except (OSError, ValueError):
        return None
    return (str(path), status.st_mtime_ns, status.st_size)


def clear_caches():
    _TAG_CACHE.clear()
    _BASIC_INFO_CACHE.clear()
    _INFO_CACHE.clear()


def forget(path):
    """Se llama tras escribir: la entrada guardada ya no describe el archivo."""
    clave = str(path)
    _TAG_CACHE.pop(clave, None)
    _BASIC_INFO_CACHE.pop(clave, None)
    _INFO_CACHE.pop(clave, None)


def _first(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]).strip() if value else ""
    return str(value).strip()


def read_tags(path):
    """Devuelve dict de tags, o None si el archivo no es legible."""
    clave = str(path)
    stamp = _stamp(path)
    guardado = _TAG_CACHE.get(clave)
    if guardado is not None and stamp is not None and guardado[0] == stamp:
        return dict(guardado[1]) if guardado[1] is not None else None

    valor = _read_tags_uncached(path)
    if stamp is not None:
        if len(_TAG_CACHE) >= _TAG_CACHE_MAX:
            _TAG_CACHE.clear()
        _TAG_CACHE[clave] = (stamp, valor)
    return dict(valor) if valor is not None else None


def _read_tags_uncached(path):
    try:
        audio = MutagenFile(path, easy=True)
    except (MutagenError, OSError, ValueError):
        return None
    if audio is None:
        return None
    stamp = _stamp(path)
    if stamp is not None:
        if len(_BASIC_INFO_CACHE) >= _BASIC_INFO_CACHE_MAX:
            _BASIC_INFO_CACHE.clear()
        _BASIC_INFO_CACHE[str(path)] = (stamp, _basic_info(audio, path))
    tags = audio.tags
    if tags is None:
        return {field: "" for field in FIELDS}
    return {field: _first(tags.get(field)) for field in FIELDS}


class _SinInterrupcion:
    """Aplaza Ctrl+C hasta que termine el bloque.

    Solo para escrituras en el MP3. Fuera de eso, el Ctrl+C tiene que cortar en
    el acto: aqui se usa en tramos de milisegundos, no en esperas de red.
    """

    def __enter__(self):
        self.pendiente = None
        self.anterior = None
        try:
            self.anterior = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._recoger)
        except (ValueError, OSError, AttributeError):
            # En un hilo secundario no se pueden cambiar las senales. Ahi no
            # llega el Ctrl+C de todas formas.
            self.anterior = None
        return self

    def _recoger(self, firma, marco):
        self.pendiente = (firma, marco)

    def __exit__(self, *_excepcion):
        if self.anterior is not None:
            try:
                signal.signal(signal.SIGINT, self.anterior)
            except (ValueError, OSError):
                pass
        if self.pendiente and callable(self.anterior):
            self.anterior(*self.pendiente)
        elif self.pendiente:
            raise KeyboardInterrupt
        return False


def save_audio(audio, path):
    """Guarda usando ID3v2.3 en los MP3.

    Mutagen escribe ID3v2.4 por defecto, y Windows Media Player, Groove, muchos
    equipos de coche y los Android antiguos no lo leen: ven el archivo pero
    ignoran las etiquetas. v2.3 lo entiende todo el mundo.
    """
    # Decide por el contenedor real, no por la extension. En la biblioteca hay
    # MP4/AAC descargados como .mp3; pasarles opciones de ID3 es incorrecto.
    from mutagen.mp3 import MP3

    with _SinInterrupcion():
        if isinstance(audio, MP3):
            audio.save(v2_version=3)
        else:
            audio.save()
    forget(path)


def write_tags(path, updates):
    """Escribe solo los campos indicados. Devuelve (ok, mensaje_error)."""
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return False, "formato no soportado"
        if audio.tags is None:
            audio.add_tags()
        for field, value in updates.items():
            if value:
                audio.tags[field] = value
            elif field in audio.tags:
                del audio.tags[field]
        save_audio(audio, path)
        return True, ""
    except (MutagenError, OSError, ValueError, KeyError) as error:
        return False, str(error)


def id3_version(path):
    """(major, minor) de la etiqueta ID3, o None si no es MP3 o no tiene."""
    from mutagen.id3 import ID3, ID3NoHeaderError

    if not str(path).lower().endswith(".mp3"):
        return None
    try:
        tags = ID3(path)
    except (ID3NoHeaderError, MutagenError, OSError, ValueError):
        return None
    version = getattr(tags, "version", None)
    return version[:2] if version else None


def downgrade_to_v23(path):
    """Reescribe la etiqueta como ID3v2.3 sin tocar los valores."""
    from mutagen.id3 import ID3, ID3NoHeaderError

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return True, ""
    except (MutagenError, OSError, ValueError) as error:
        return False, str(error)
    try:
        tags.update_to_v23()
        tags.save(path, v2_version=3)
        forget(path)
        return True, ""
    except (MutagenError, OSError, ValueError) as error:
        return False, str(error)


class Backup:
    """Guarda los tags originales antes de la primera escritura.

    Se vuelca a disco cada pocos registros, no solo al terminar: una pasada con
    --online dura horas, y si el proceso muere de golpe sin ese volcado los
    archivos ya escritos se quedarian sin forma de deshacerse.
    """

    FLUSH_EVERY = 50

    def __init__(self, backup_dir):
        self.dir = Path(backup_dir)
        self.entries = {}
        self.path = None
        self.stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.pending = 0

    def record(self, path, original):
        self.entries[str(path)] = original
        self.pending += 1
        if self.pending >= self.FLUSH_EVERY:
            self.save()

    def save(self):
        if not self.entries:
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.path is None:
            self.path = self.dir / ("tags-backup-%s.json" % self.stamp)
        payload = {"created": self.stamp, "count": len(self.entries),
                   "tags": self.entries}
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        tmp.replace(self.path)
        self.pending = 0
        return self.path


def restore(backup_path):
    """Rehace los tags desde un backup. Devuelve (restaurados, fallidos)."""
    with open(backup_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    restored, failed = 0, []
    for raw_path, original in payload.get("tags", {}).items():
        path = Path(raw_path)
        if not path.exists():
            failed.append((raw_path, "no existe"))
            continue
        ok, error = write_tags(path, original)
        if ok:
            restored += 1
        else:
            failed.append((raw_path, error))
    return restored, failed


# ---------------------------------------------------------------------------
# Informacion de audio y portadas
# ---------------------------------------------------------------------------

def _basic_info(audio, path):
    info = getattr(audio, "info", None)
    try:
        size = path.stat().st_size
    except (OSError, ValueError):
        size = 0
    return {
        "duration": float(getattr(info, "length", 0.0) or 0.0),
        "bitrate": int(getattr(info, "bitrate", 0) or 0),
        "size": size,
    }


def read_basic_audio_info(path):
    """Duracion/bitrate sin volver a abrir un archivo cuyos tags ya se leyeron."""
    clave = str(path)
    stamp = _stamp(path)
    guardado = _BASIC_INFO_CACHE.get(clave)
    if guardado is not None and stamp is not None and guardado[0] == stamp:
        return dict(guardado[1]) if guardado[1] is not None else None

    try:
        audio = MutagenFile(path, easy=True)
    except (MutagenError, OSError, ValueError):
        audio = None
    valor = _basic_info(audio, path) if audio is not None else None
    if stamp is not None:
        if len(_BASIC_INFO_CACHE) >= _BASIC_INFO_CACHE_MAX:
            _BASIC_INFO_CACHE.clear()
        _BASIC_INFO_CACHE[clave] = (stamp, valor)
    return dict(valor) if valor is not None else None

def read_audio_info(path):
    """Devuelve duracion en segundos, bitrate y tamano de la portada embebida.

    cover_bytes vale 0 tanto si no hay portada como si la hay pero esta vacia,
    que es un caso real en esta biblioteca.
    """
    clave = str(path)
    stamp = _stamp(path)
    guardado = _INFO_CACHE.get(clave)
    if guardado is not None and stamp is not None and guardado[0] == stamp:
        _INFO_CACHE.move_to_end(clave)
        return dict(guardado[1]) if guardado[1] is not None else None

    valor = _read_audio_info_uncached(path)
    if stamp is not None:
        _INFO_CACHE[clave] = (stamp, valor)
        if valor is not None:
            _BASIC_INFO_CACHE[clave] = (
                stamp, {field: valor[field]
                        for field in ("duration", "bitrate", "size")})
        while len(_INFO_CACHE) > _INFO_CACHE_MAX:
            _INFO_CACHE.popitem(last=False)
    return dict(valor) if valor is not None else None


def _read_audio_info_uncached(path):
    try:
        audio = MutagenFile(path)
    except (MutagenError, OSError, ValueError):
        return None
    if audio is None:
        return None

    basic = _basic_info(audio, path)
    cover = 0
    cover_data = b""
    tags = audio.tags
    if tags is not None:
        try:
            for key in tags.keys():
                if key.startswith("APIC"):
                    data = tags[key].data or b""
                    if len(data) > cover:
                        cover, cover_data = len(data), data
        except (AttributeError, TypeError):
            pass
        if not cover and hasattr(tags, "get"):
            mp4_covers = tags.get("covr") or []
            if mp4_covers:
                best = max(mp4_covers, key=len)
                cover, cover_data = len(best), bytes(best)
            pictures = getattr(audio, "pictures", None)
            if not cover and pictures:
                best = max(pictures, key=lambda pic: len(pic.data or b""))
                cover, cover_data = len(best.data or b""), (best.data or b"")

    return {
        "duration": basic["duration"],
        "bitrate": basic["bitrate"],
        "cover_bytes": cover,
        "cover_data": cover_data,
        "size": basic["size"],
    }


def embed_cover(path, image_bytes, mime="image/jpeg"):
    """Incrusta la portada como caratula frontal. Devuelve (ok, error)."""
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.wave import WAVE

    try:
        audio = MutagenFile(path)
        if audio is None:
            return False, "formato no soportado"

        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()
            image_format = (MP4Cover.FORMAT_PNG if mime == "image/png"
                            else MP4Cover.FORMAT_JPEG)
            audio.tags["covr"] = [MP4Cover(image_bytes,
                                           imageformat=image_format)]
            with _SinInterrupcion():
                audio.save()
            forget(path)
            return True, ""

        if isinstance(audio, FLAC):
            picture = Picture()
            picture.type = 3
            picture.mime = mime
            picture.desc = "Cover"
            picture.data = image_bytes
            audio.clear_pictures()
            audio.add_picture(picture)
            with _SinInterrupcion():
                audio.save()
            forget(path)
            return True, ""

        if not isinstance(audio, (MP3, WAVE)):
            return False, "portada no soportada para %s" % type(audio).__name__

        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for key in [k for k in tags.keys() if k.startswith("APIC")]:
            del tags[key]
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
        tags.save(path, v2_version=3)
        forget(path)
        return True, ""
    except (MutagenError, OSError, ValueError) as error:
        return False, str(error)


def remove_cover(path):
    """Quita las caratulas embebidas. Devuelve (ok, error)."""
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4

    try:
        audio = MutagenFile(path)
        if isinstance(audio, MP4):
            if audio.tags and "covr" in audio.tags:
                del audio.tags["covr"]
                audio.save()
                forget(path)
            return True, ""
        if isinstance(audio, FLAC):
            audio.clear_pictures()
            with _SinInterrupcion():
                audio.save()
            forget(path)
            return True, ""
    except (MutagenError, OSError, ValueError) as error:
        return False, str(error)

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return True, ""
    except (MutagenError, OSError, ValueError) as error:
        return False, str(error)

    keys = [k for k in tags.keys() if k.startswith("APIC")]
    if not keys:
        return True, ""
    for key in keys:
        del tags[key]
    try:
        tags.save(path, v2_version=3)
        forget(path)
        return True, ""
    except (MutagenError, OSError, ValueError) as error:
        return False, str(error)

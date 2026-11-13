"""MP4/AAC descargados con extension .mp3, convertidos a MP3 de verdad.

Los reproductores se fian de la extension. Un .mp3 que por dentro es MP4 suena,
porque el decodificador se las apana, pero sus etiquetas son atomos MP4 que
nadie busca en un .mp3: sale sin datos ni portada. Y si alguien le anade una
cabecera ID3, el reproductor da por hecho que es MP3 y revienta. Asi se
rompieron 25 archivos de la biblioteca.

Se convierte a LAME V0 (unos 245 kbps) y el original solo se sustituye si el
resultado dura lo mismo (un segundo de margen) y se decodifica entero sin un
solo error. Los tags y la portada se copian del original. El original se guarda
en backups/ y "deshacer" lo devuelve a su sitio.
"""

import shutil
import subprocess
from pathlib import Path

from .tags import (FIELDS, _SinInterrupcion, embed_cover, forget,
                   read_audio_info, read_tags, write_tags)

MARGEN_DURACION = 1.0


def es_mp4_disfrazado(path):
    """Un .mp3 cuyo contenido empieza como un MP4 ("....ftyp")."""
    if Path(path).suffix.lower() != ".mp3":
        return False
    try:
        with open(path, "rb") as handle:
            cabecera = handle.read(12)
    except OSError:
        return False
    return len(cabecera) >= 8 and cabecera[4:8] == b"ftyp"


def esta_vacio(path):
    try:
        return Path(path).stat().st_size == 0
    except OSError:
        return False


def _ffmpeg():
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _ejecutar(orden):
    return subprocess.run(orden, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _duracion_mp3(path):
    from mutagen.mp3 import MP3

    try:
        return float(MP3(path).info.length)
    except Exception:
        return 0.0


def _destino_libre(ruta):
    candidata, numero = ruta, 2
    while candidata.exists():
        candidata = ruta.with_name("%s (%d)%s" % (ruta.stem, numero, ruta.suffix))
        numero += 1
    return candidata


def convertir(path, carpeta_copias):
    """Convierte un MP4 disfrazado en MP3. Devuelve (copia_del_original, error)."""
    path = Path(path)
    try:
        ffmpeg = _ffmpeg()
    except ImportError:
        return None, "falta imageio-ffmpeg (pip install -r requirements.txt)"

    tags = read_tags(path) or {}
    info = read_audio_info(path) or {}
    duracion = float(info.get("duration") or 0)
    portada = info.get("cover_data") or b""
    temporal = path.with_name(path.stem + ".convirtiendo.mp3")
    try:
        hecho = _ejecutar([ffmpeg, "-hide_banner", "-v", "error", "-y",
                           "-i", str(path), "-map", "0:a:0", "-vn",
                           "-map_metadata", "-1", "-c:a", "libmp3lame",
                           "-q:a", "0", str(temporal)])
        if hecho.returncode != 0 or not temporal.exists():
            return None, "ffmpeg: %s" % (hecho.stderr.strip()[:200]
                                         or hecho.returncode)
        nueva = _duracion_mp3(temporal)
        if not duracion or abs(nueva - duracion) > MARGEN_DURACION:
            return None, ("la duracion no cuadra (%.1f s frente a %.1f s)"
                          % (nueva, duracion))
        prueba = _ejecutar([ffmpeg, "-hide_banner", "-v", "error",
                            "-i", str(temporal), "-f", "null", "-"])
        if prueba.returncode != 0 or prueba.stderr.strip():
            return None, ("no se decodifica entero: %s"
                          % prueba.stderr.strip()[:200])

        campos = {campo: tags[campo] for campo in FIELDS if tags.get(campo)}
        if campos:
            ok, error = write_tags(temporal, campos)
            if not ok:
                return None, "no se pudieron copiar los tags: %s" % error
        if portada:
            from .covers import guess_mime

            ok, error = embed_cover(temporal, portada, guess_mime(portada))
            if not ok:
                return None, "no se pudo copiar la portada: %s" % error

        carpeta_copias.mkdir(parents=True, exist_ok=True)
        copia = _destino_libre(carpeta_copias / path.name)
        shutil.copy2(path, copia)
        with _SinInterrupcion():
            temporal.replace(path)
        forget(path)
        return copia, ""
    finally:
        if temporal.exists():
            try:
                temporal.unlink()
            except OSError:
                pass

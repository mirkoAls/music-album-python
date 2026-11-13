"""Gestor de metadatos para una biblioteca musical.

    python main.py todo            # TODA la cadena, en orden (lo habitual)

Y cada paso por separado, si hace falta:

    python main.py nombres         # quita emojis y ruido del nombre
    python main.py artistas        # artistas rellenos pero mal (canales, sellos)
    python main.py compatibilidad  # pasa las etiquetas a ID3v2.3
    python main.py scan            # limpia y completa los tags
    python main.py portadas        # descarga caratulas
    python main.py duplicados      # encuentra repetidas
    python main.py generos         # avisa de canciones en la carpeta equivocada
    python main.py restaurar / deshacer
"""

import argparse
import csv
import hashlib
import os
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests

from tagger import artists as artists_mod
from tagger import conversion
from tagger import covers as covers_mod
from tagger.correcciones import (CAMPOS_DE_TAG,
                                 compartidas as correcciones_compartidas)
from tagger.duplicates import find_duplicates, move_duplicates, why_keep
from tagger.filenames import folder_hints, set_category_folders
from tagger.genre_check import check as genre_check
from tagger.genres import genres_by_artist
from tagger.musicbrainz import APP_NAME, APP_VERSION, CONTACT
from tagger.pipeline import (CORRECTIONS_PATH, analyze, correction_for,
                             identity_for, online_artist_hints)
from tagger.text import collapse, main_artist, norm as norm_texto
from tagger.providers import Providers
from tagger.renaming import apply_renames, plan_renames, undo
from tagger.report import Report, setup_console
from tagger.tags import (Backup, FIELDS, downgrade_to_v23, embed_cover,
                         id3_version, read_audio_info, read_tags, remove_cover,
                         restore, scan_library, write_tags)
from tagger.verification import MANUAL as FIJADA, VerificationLedger
from tagger.candado import BibliotecaOcupada, Candado

DEFAULT_ROOT = Path(r"D:\Musica")
WORK_DIR = Path(__file__).resolve().parent
LOG_DIR = WORK_DIR / "logs"
BACKUP_DIR = WORK_DIR / "backups"
CACHE_DIR = WORK_DIR / "cache"
REPORT_DIR = WORK_DIR / "informes"


# ---------------------------------------------------------------------------
# Utilidades comunes
# ---------------------------------------------------------------------------

def add_common(parser):
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="carpeta raiz (por defecto %s)" % DEFAULT_ROOT)
    parser.add_argument("--carpeta", default="",
                        help='limita a una carpeta: "pop" o "electro/hardwell"')
    parser.add_argument("--limit", type=int, default=0, help="solo N archivos")


def iter_files(root, folder="", limit=0, skip_quarantine=True):
    count = 0
    for path in scan_library(root):
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if skip_quarantine and parts and parts[0] == "_duplicados":
            continue
        if folder:
            # Admite "pop" y tambien "electro/hardwell": se comparan las primeras
            # partes de la ruta. La raiz sigue siendo la biblioteca entera, para
            # que las pistas de genero y artista no se pierdan.
            wanted = tuple(x.lower() for x in Path(folder).parts if x not in (".", ""))
            if len(parts) <= len(wanted):
                continue
            if tuple(x.lower() for x in parts[:len(wanted)]) != wanted:
                continue
        yield path
        count += 1
        if limit and count >= limit:
            return


# Hilos para el trabajo de lectura. Ni uno por nucleo: lo que se espera es
# disco y red, no CPU, y pasarse solo anade contencion.
LECTORES = min(8, (os.cpu_count() or 4) + 2)


def analizar_en_paralelo(rutas, trabajo):
    """Analiza varios archivos a la vez y devuelve los resultados EN ORDEN.

    El analisis solo lee: tags, duracion y las fuentes (que llevan su propio
    freno por servicio). Lo que escribe se hace despues, en serie, con esta
    lista ya ordenada.
    """
    rutas = list(rutas)
    if len(rutas) <= 1:
        return [(r, trabajo(r)) for r in rutas]
    with ThreadPoolExecutor(max_workers=LECTORES,
                            thread_name_prefix="analisis") as pool:
        return list(zip(rutas, pool.map(trabajo, rutas)))


def progress(index, total, extra=""):
    if index % 100 == 0 or index == total:
        print("  %d/%d%s" % (index, total, extra), flush=True)


def _short_time(seconds):
    """Duracion compacta para que la barra quepa en una consola normal."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600,
                                  (seconds % 3600) // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def progress_bar(done, total, started, current=""):
    """Barra para pasos lentos: completado, tiempo usado, ETA y archivo actual."""
    if total <= 0:
        return
    done = max(0, min(done, total))
    ratio = done / float(total)
    width = 12
    filled = int(round(width * ratio))
    bar = "#" * filled + "-" * (width - filled)
    elapsed = time.monotonic() - started
    if done == 0:
        remaining = "--:--"
    elif done >= total:
        remaining = "0:00"
    else:
        remaining = _short_time((elapsed / done) * (total - done))
    name = Path(current).name if current else ""
    if len(name) > 28:
        name = name[:25] + "..."
    print("\r  [%s] %3d%% %d/%d | %s / ~%s | %-28s" %
          (bar, int(round(ratio * 100)), done, total,
           _short_time(elapsed), remaining, name),
          end="", flush=True)


def write_csv(path, header, rows):
    if not rows:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_log(kind, payload):
    carpeta = LOG_DIR / kind
    carpeta.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = carpeta / ("%s-%s.json" % (kind, stamp))
    body = {"created": stamp, "kind": kind}
    body.update(payload)
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, indent=1)
    return log_path


def safe_name(folder):
    """'electro/hardwell' no vale como nombre de archivo."""
    return re.sub(r'[\\/:*?"<>|]', "-", folder) if folder else "todo"


def informe_de(prefijo, carpeta, ext):
    """informes/<carpeta>/<prefijo>-<carpeta>.<ext>: los de una carpeta van juntos."""
    nombre = safe_name(carpeta)
    return REPORT_DIR / nombre / ("%s-%s.%s" % (prefijo, nombre, ext))


def write_txt(path, titulo, lineas):
    """Lista en texto plano de lo que no se pudo resolver, para mirarlo a mano."""
    if not lineas:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("%s\n%s\n\n" % (titulo, "=" * len(titulo)))
        handle.write("%d casos.\n\n" % len(lineas))
        handle.write("\n".join(lineas))
        handle.write("\n")
    return path


def show_failures(failures, limit=10):
    for item, error in failures[:limit]:
        print("  FALLO %s -> %s" % (item, error))
    if len(failures) > limit:
        print("  ... y %d fallos mas" % (len(failures) - limit))


def configure_folder_categories(files, root):
    """Impide usar carpetas tematicas como si fueran artistas."""
    categories = artists_mod.category_folders(files, root)
    set_category_folders(categories)
    return categories


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def contexto_biblioteca(root, mostrar=True):
    """Lo que solo se sabe mirando la biblioteca ENTERA, calculado una vez.

    Un tag que acompana a artistas distintos es el nombre de un canal, y eso no
    se ve dentro de una sola carpeta: juzgando solo ahi, un canal que sale una
    vez en cumbia y otra en rock pasaria por artista bueno en las dos.

    Antes cada paso lo reconstruia por su cuenta, asi que recorrer la
    biblioteca por carpetas lo repetia treinta y dos veces. Ahora se arma una
    vez por carpeta y lo comparten "scan" y "portadas".
    """
    todos = list(iter_files(root))
    categories = configure_folder_categories(todos, root)
    category_suspects = artists_mod.category_impostor_paths(
        todos, root, categories)
    suspects = artists_mod.suspect_paths(
        artists_mod.judge(artists_mod.collect(todos)))
    suspects.update(category_suspects)
    known = artists_mod.known_names(todos, root)
    artist_genres = genres_by_artist(todos)
    artist_spellings = artists_mod.canonical_artist_spellings(todos)
    album_spellings = artists_mod.canonical_album_spellings(todos)

    if mostrar:
        print("Contexto de la biblioteca (%d archivos)" % len(todos))
        if categories:
            print("  carpetas tematicas (no son artistas): %s" %
                  ", ".join(sorted(categories, key=str.casefold)))
        print("  artistas no fiables                 : %d" % len(suspects))
        print("  artistas que la biblioteca conoce   : %d" % len(known))
        print("  artistas con genero conocido        : %d" % len(artist_genres))
        print("  albumes escritos de dos maneras     : %d\n" % len(album_spellings))

    return {
        "files": todos,
        "categories": categories,
        "category_suspects": category_suspects,
        "suspects": suspects,
        "known": known,
        "artist_genres": artist_genres,
        "artist_spellings": artist_spellings,
        "album_spellings": album_spellings,
    }


def _correccion_pendiente(path, tags):
    """Campos de correcciones.json que el archivo todavia no tiene escritos.

    La marca de "fijada" protege lo revisado de las fuentes, y eso esta bien.
    Pero no puede tapar al propio archivo de correcciones: si se amplia una
    entrada ya aplicada (se le anade el album, la fecha...), lo nuevo tiene que
    llegar al MP3. Antes se ignoraba en silencio.

    Se compara igual que el pipeline al decidir si hay algo que proponer: el
    valor ya normalizado contra el tag actual.
    """
    fijos = {campo: collapse(valor)
             for campo, valor in correction_for(path).items()
             if campo in CAMPOS_DE_TAG}
    if not fijos:
        return False
    if fijos.get("artist") and not fijos.get("albumartist"):
        fijos["albumartist"] = main_artist(fijos["artist"])
    actuales = tags or {}
    return any(valor != actuales.get(campo, "")
               for campo, valor in fijos.items())


def run_scan(args):
    # Todo lo que sigue mira la biblioteca ENTERA, aunque solo se vaya a tocar
    # una carpeta. Un tag que acompana a artistas distintos es el nombre de un
    # canal, y eso solo se ve comparando unos archivos con otros: juzgando solo
    # dentro de la carpeta, un canal que aparece una vez en cumbia y otra en
    # rock pasaria por artista bueno en las dos.
    contexto = getattr(args, "contexto", None)
    if contexto is None:
        # Sin contexto compartido (comando suelto) se arma aqui. Con
        # --no-revisar-artistas se salta la parte cara, que es juzgar los tags
        # de artista de toda la biblioteca.
        todos = list(iter_files(args.root))
        categories = configure_folder_categories(todos, args.root)
        if categories:
            print("Carpetas tematicas (no son artistas): %s" %
                  ", ".join(sorted(categories, key=str.casefold)))
        category_suspects = artists_mod.category_impostor_paths(
            todos, args.root, categories)
        suspects = set(category_suspects)
        if not getattr(args, "no_revisar_artistas", False):
            print("Revisando artistas dudosos en %d archivos..." % len(todos))
            suspects = artists_mod.suspect_paths(
                artists_mod.judge(artists_mod.collect(todos)))
            suspects.update(category_suspects)
            print("  artistas no fiables: %d\n" % len(suspects))
        print("Reuniendo generos por artista...")
        artist_genres = genres_by_artist(todos)
        print("  artistas con genero conocido: %d" % len(artist_genres))
        known = artists_mod.known_names(todos, args.root)
        print("  artistas que la biblioteca ya conoce: %d\n" % len(known))
        artist_spellings = artists_mod.canonical_artist_spellings(todos)
        album_spellings = artists_mod.canonical_album_spellings(todos)
    else:
        todos = contexto["files"]
        set_category_folders(contexto["categories"])
        suspects = contexto["suspects"]
        known = contexto["known"]
        artist_genres = contexto["artist_genres"]
        artist_spellings = contexto["artist_spellings"]
        album_spellings = contexto["album_spellings"]

    client = None
    if args.online:
        client = Providers(CACHE_DIR, min_score=args.min_score)
        client.consensus_mode = args.consenso
        if not (client.deezer or client.itunes or client.musicbrainz):
            print("ERROR: no hay ninguna fuente disponible.")
            return 1

    # Canciones que ya quedaron completas y verificadas en una pasada
    # anterior. Mientras no cambien sus tags ni su nombre, no hay nada que
    # preguntar: es lo que evita gastar la red en el 90% que ya esta bien.
    verificadas = VerificationLedger(CACHE_DIR / "verificadas.json")

    report = Report(args.csv)
    backup = Backup(BACKUP_DIR)
    written, failed, sin_datos = 0, [], []
    saltadas = fijadas = 0

    files = list(iter_files(args.root, args.carpeta, args.limit))
    total = len(files)
    print("Archivos a analizar: %d" % total)
    print("MODO ESCRITURA: se guarda un backup antes de tocar nada."
          if args.apply else "Modo informe (no se modifica nada).")
    print("  Barra: tiempo usado / tiempo restante aproximado")

    started = time.monotonic()
    processed = 0
    def estudiar(path):
        """Todo lo que solo LEE. Se puede hacer en paralelo sin riesgo."""
        tags_actuales = read_tags(path)
        # Revisada a mano y sin tocar desde entonces: no se cambia nada, ni
        # siquiera la limpieza local. Es lo que protege lo arreglado a mano.
        if (verificadas.is_manual(path, tags_actuales)
                and not _correccion_pendiente(path, tags_actuales)):
            return FIJADA, [], ""
        # Ya verificada y sin tocar: se limpia en local igual que siempre,
        # pero sin consultar nada. La limpieza es determinista y gratis; lo
        # caro es la red.
        ya_estaba = (client is not None
                     and verificadas.is_verified(path, tags_actuales))
        proposals, note = analyze(
            path, args.root, mb_client=None if ya_estaba else client,
            suspect_artists=suspects,
            verify=args.online and args.verificar,
            artist_genres=artist_genres, known=known,
            artist_spellings=artist_spellings,
            album_spellings=album_spellings)
        return ya_estaba, proposals, note

    # De cuantos en cuantos se analiza. El lote se estudia a la vez y despues
    # se escribe en orden; mas grande no acelera y retrasa el guardado.
    LOTE = LECTORES * 4

    try:
        index = 0
        for comienzo in range(0, total, LOTE):
            lote = files[comienzo:comienzo + LOTE]
            progress_bar(processed, total, started, lote[0])
            estudiados = analizar_en_paralelo(lote, estudiar)

            for path, (ya_estaba, proposals, note) in estudiados:
                index += 1
                processed = index
                if ya_estaba == FIJADA:
                    fijadas += 1
                    report.add(path, [], "")
                    continue
                if ya_estaba:
                    saltadas += 1
                report.add(path, proposals, note)

                if client and not ya_estaba:
                    final = dict(read_tags(path) or {})
                    for proposal in proposals:
                        if proposal.writable(args.include_doubtful):
                            final[proposal.field] = proposal.new
                    faltan = [f for f in ("album", "date", "genre") if not final.get(f)]
                    if faltan:
                        artista, titulo, _ = identity_for(path, args.root,
                                                          suspects, known=known)
                        sin_datos.append(
                            "%s\n    busque: artista=%r titulo=%r\n    sigue sin: %s"
                            % (path, artista, titulo, ", ".join(faltan)))

                if not args.apply:
                    continue

                updates = {}
                for proposal in proposals:
                    if proposal.writable(args.include_doubtful):
                        updates[proposal.field] = proposal.new

                original = read_tags(path)
                if original is None:
                    continue
                if updates:
                    backup.record(path, {f: original.get(f, "") for f in FIELDS})
                    ok, error = write_tags(path, updates)
                    if ok:
                        written += 1
                    else:
                        failed.append((str(path), error))
                        continue

                # Se apunta lo que queda escrito en el archivo, no lo que se
                # propuso: si algo no se escribio por ser dudoso, la cancion sigue
                # incompleta y hay que volver a mirarla la proxima vez. Solo se
                # apunta con --apply, porque en modo informe no se escribio nada.
                finales = dict(original)
                finales.update(updates)
                if correction_for(path):
                    # Lo escrito desde correcciones.json queda fijado: en
                    # adelante ninguna fuente lo vuelve a tocar.
                    verificadas.mark_manual(path, finales)
                elif client and not ya_estaba:
                    verificadas.record(path, finales, "online")
    except KeyboardInterrupt:
        print("\nInterrumpido. Guardando lo hecho...")
    finally:
        progress_bar(processed, total, started,
                     "Terminado" if processed == total else "Interrumpido")
        print("")
        if client:
            client.close()
        verificadas.flush()
        backup_path = backup.save()

    csv_path = report.save()
    print(report.summary())
    if fijadas:
        print("  Revisadas a mano (no se tocan): %d" % fijadas)
    if client:
        if saltadas:
            print("  Ya verificadas en una pasada anterior (sin consultar): %d"
                  % saltadas)
        print(client.summary())
        txt = write_txt(informe_de("sin-datos", args.carpeta, "txt"),
                        "Sin album, fecha o genero tras consultar las fuentes",
                        sin_datos)
        if txt:
            print("\n  Sin resolver (%d): %s" % (len(sin_datos), txt))
    if csv_path:
        print("\n  Informe: %s" % csv_path)
    if args.apply:
        print("  Archivos escritos: %d" % written)
        if backup_path:
            print("  Backup:  %s" % backup_path)
            print('  Deshacer: python main.py restaurar "%s"' % backup_path)
        show_failures(failed)
    return 0


# ---------------------------------------------------------------------------
# duplicados
# ---------------------------------------------------------------------------

def run_duplicates(args):
    files = list(iter_files(args.root, args.carpeta, args.limit))
    total = len(files)
    print("Analizando %d archivos (artista + titulo + duracion)..." % total)

    groups = find_duplicates(files, args.root,
                             progress=lambda i: progress(i, total))

    rows = []
    for number, group in enumerate(groups, start=1):
        entries = group["entries"]
        keeper = entries[0]
        for position, entry in enumerate(entries):
            rows.append([
                number,
                "CONSERVAR" if position == 0 else "duplicado",
                str(entry["path"]),
                "%.0f" % entry["duration"],
                entry["bitrate"] // 1000,
                "si" if entry["cover_bytes"] > 1024 else "no",
                round(entry["size"] / 1024 / 1024, 1),
                why_keep(keeper, entries[1:]) if position == 0 else "",
            ])

    csv_path = write_csv(args.csv, ["grupo", "accion", "archivo", "seg", "kbps",
                                    "portada", "MB", "motivo"], rows)

    affected = sum(len(g["entries"]) for g in groups)
    cross = 0
    for group in groups:
        folders = set()
        for entry in group["entries"]:
            try:
                folders.add(entry["path"].relative_to(args.root).parts[0])
            except (ValueError, IndexError):
                pass
        if len(folders) > 1:
            cross += 1

    print("\n" + "=" * 66)
    print("  Grupos de duplicados : %d" % len(groups))
    print("  Archivos implicados  : %d" % affected)
    print("  Sobran (a mover)     : %d" % (affected - len(groups)))
    print("  Repartidos entre carpetas distintas: %d grupos" % cross)
    print("=" * 66)
    if csv_path:
        print("  Informe: %s" % csv_path)

    if not args.apply:
        print("\n  Nada movido. Revisa el CSV y repite con --apply para mover")
        print("  los sobrantes a %s" % (args.root / "_duplicados"))
        return 0

    moved, log_path, failures = move_duplicates(groups, args.root, LOG_DIR / "movidos")
    print("\n  Movidos a _duplicados: %d" % moved)
    if log_path:
        print("  Log: %s" % log_path)
        print('  Deshacer: python main.py deshacer "%s"' % log_path)
    show_failures(failures)
    return 0


# ---------------------------------------------------------------------------
# nombres
# ---------------------------------------------------------------------------

def _al_renombrar():
    """Lo que va atado a la ruta de un archivo y tiene que seguirle.

    La marca de la portada, la ficha revisada a mano y la entrada de
    correcciones.json. Estaban atadas a la ruta, y cada renombrado las perdia:
    asi se quedaron sin proteger 15 portadas elegidas a mano.
    """
    ledger = covers_mod.CoverLedger(CACHE_DIR / "portadas.json")
    verificadas = VerificationLedger(CACHE_DIR / "verificadas.json")
    correcciones = correcciones_compartidas(CORRECTIONS_PATH)

    def mover(origen, destino):
        ledger.move(origen, destino)
        verificadas.move(origen, destino, read_tags(destino))
        correcciones.mover(origen, destino)

    def guardar():
        ledger.flush()
        verificadas.flush()
        correcciones.guardar()

    return mover, guardar


def run_names(args):
    files = list(iter_files(args.root, args.carpeta, args.limit))
    planned = plan_renames(files)

    rows = [[str(source), source.name, target.name] for source, target in planned]
    csv_path = write_csv(args.csv, ["archivo", "nombre_actual", "nombre_nuevo"], rows)

    print("\n  Archivos revisados : %d" % len(files))
    print("  Nombres a limpiar  : %d" % len(planned))
    for source, target in planned[:12]:
        print("    %s" % source.name)
        print("      -> %s" % target.name)
    if len(planned) > 12:
        print("    ... y %d mas (ver CSV)" % (len(planned) - 12))
    if csv_path:
        print("\n  Informe: %s" % csv_path)

    if not args.apply:
        print("\n  Nada renombrado. Repite con --apply para aplicarlo.")
        return 0

    mover, guardar = _al_renombrar()
    done, log_path, failures = apply_renames(planned, LOG_DIR / "renombrados", on_rename=mover)
    guardar()
    print("\n  Renombrados: %d" % done)
    if log_path:
        print("  Log: %s" % log_path)
        print('  Deshacer: python main.py deshacer "%s"' % log_path)
    show_failures(failures)
    return 0


# ---------------------------------------------------------------------------
# portadas
# ---------------------------------------------------------------------------

def _portada_declarada(origen):
    """Bytes de una portada elegida a mano: una URL o la ruta de una imagen.

    Las de internet se guardan en cache/portadas-manuales para no bajarlas en
    cada pasada.
    """
    if re.match(r"https?://", origen, re.IGNORECASE):
        guardada = CACHE_DIR / "portadas-manuales" / (
            hashlib.sha1(origen.encode("utf-8")).hexdigest() + ".img")
        if guardada.exists():
            datos = guardada.read_bytes()
        else:
            try:
                respuesta = requests.get(origen, timeout=30, headers={
                    "User-Agent": "%s/%s ( %s )" % (APP_NAME, APP_VERSION,
                                                    CONTACT)})
            except requests.RequestException as error:
                return None, str(error)
            if not respuesta.ok:
                return None, "HTTP %d" % respuesta.status_code
            datos = respuesta.content
            if covers_mod.image_size(datos)[0]:
                guardada.parent.mkdir(parents=True, exist_ok=True)
                guardada.write_bytes(datos)
    else:
        ruta = Path(origen)
        if not ruta.is_absolute():
            ruta = WORK_DIR / ruta
        try:
            datos = ruta.read_bytes()
        except OSError as error:
            return None, str(error)
    if not covers_mod.image_size(datos)[0]:
        return None, "no es una imagen JPEG ni PNG"
    return datos, ""


def _buscador_de_fotos(providers, biblioteca):
    """Foto del artista para los temas que se quedan sin ninguna portada.

    La regla de siempre: la del single, si no la del disco, y si no hay ni una
    ni otra, una foto del artista en buena calidad. Solo cuando es sin duda el
    mismo: otra cancion de la biblioteca de ese artista tiene que estar entre
    sus temas en Deezer.
    """
    if providers.deezer is None:
        return None
    titulos, fotos = None, {}

    def buscar(nombre):
        nonlocal titulos
        clave = norm_texto(nombre)
        if not clave:
            return None
        if clave not in fotos:
            if titulos is None:
                titulos = defaultdict(set)
                for otra in biblioteca:
                    tags = read_tags(otra) or {}
                    artista = norm_texto(main_artist(tags.get("artist", "")))
                    if artista and tags.get("title"):
                        titulos[artista].add(tags["title"])
            with providers._source_locks["deezer"]:
                url = providers.deezer.artist_picture(
                    nombre, sorted(titulos.get(clave, ())))
            datos = providers._fetch_one({}, "deezer", url) if url else None
            fotos[clave] = (datos if datos and not covers_mod.bad_cover(datos)
                            else None)
        return fotos[clave]

    return buscar


def run_covers(args):
    files = list(iter_files(args.root, args.carpeta, args.limit))

    # Sin --forzar se miran las que faltan y ademas las claramente malas: vacias,
    # diminutas o no cuadradas, que suelen ser miniaturas de video de 16:9.
    ledger = covers_mod.CoverLedger(CACHE_DIR / "portadas.json")
    rehacer = getattr(args, "rehacer", False)

    # Portadas declaradas en correcciones.json: las eligio una persona y van
    # aparte. Se ponen si el archivo no tiene ya esa, y quedan como "elegida a
    # mano".
    correcciones = correcciones_compartidas(CORRECTIONS_PATH)
    declaradas = {}
    for ruta in files:
        origen = correcciones.para(ruta).get("portada")
        if origen:
            declaradas[ruta] = origen
    por_poner = [
        ruta for ruta in declaradas
        if not (ledger.fuente(ruta) == covers_mod.MANUAL_SOURCE
                and ledger.already_done(
                    ruta, (read_audio_info(ruta) or {}).get("cover_data") or b""))]

    # Portadas que se repiten en discos distintos: son restos de un reparto
    # anterior, no la caratula de cada tema. Se anotan aqui para poder
    # corregirlas, porque no estan en el registro y sin esto pasarian por
    # portadas tuyas de toda la vida.
    huellas = defaultdict(set)
    for ruta in files:
        datos = (read_audio_info(ruta) or {}).get("cover_data") or b""
        if not datos:
            continue
        etiquetas = read_tags(ruta) or {}
        huellas[hashlib.sha1(datos).hexdigest()].add(
            norm_texto(etiquetas.get("album", "")))
    repartidas = {firma for firma, albumes in huellas.items()
                  if len({x for x in albumes if x}) >= 3}
    if repartidas:
        print("  Portadas repartidas entre discos distintos: %d imagenes"
              % len(repartidas))

    pending, motivos, ya_puestas = [], {}, 0
    for ruta in files:
        if ruta in declaradas:
            continue
        info = read_audio_info(ruta)
        if covers_mod.needs_cover(info):
            pending.append(ruta)
            motivos[str(ruta)] = "no tiene"
            continue
        actual = (info or {}).get("cover_data") or b""
        motivo = covers_mod.bad_cover(actual)
        if motivo:
            pending.append(ruta)
            motivos[str(ruta)] = motivo
        elif getattr(args, "forzar", False):
            # La portada que puso el programa en una pasada anterior y sigue
            # intacta no se vuelve a descargar: es la misma consulta y la misma
            # imagen. Cualquier otra si se comprueba. Tambien las suyas que se
            # quedaron pequenas: son de cuando se pedian a 600 px.
            pequena = (covers_mod.min_side(actual) < covers_mod.LADO_HD
                       and ledger.placed_by_program(ruta))
            # La foto del artista es un apano: se vuelve a buscar la de verdad
            # en cada pasada, no se da por hecha.
            provisional = (ledger.fuente(ruta)
                           == covers_mod.ARTIST_PHOTO_SOURCE)
            if (not rehacer and not pequena and not provisional
                    and ledger.already_done(ruta, actual)):
                ya_puestas += 1
                continue
            pending.append(ruta)
            motivos[str(ruta)] = (
                "foto del artista, se busca la de verdad" if provisional
                else "pequena, se busca en grande" if pequena
                else "se busca la del album real")

    print("  Archivos revisados   : %d" % len(files))
    if ya_puestas:
        print("  Ya puestas por el programa (no se vuelven a bajar): %d" % ya_puestas)
    print("  A procesar           : %d" % len(pending))
    for motivo in sorted(set(motivos.values())):
        print("      %-22s %d" % (motivo,
              sum(1 for m in motivos.values() if m == motivo)))

    if declaradas:
        print("  Declaradas en correcciones.json: %d (por poner: %d)"
              % (len(declaradas), len(por_poner)))

    if not pending and not por_poner:
        return 0
    if not args.apply:
        print("\n  Nada descargado. Repite con --apply para bajar e incrustar.")
        return 0

    # Las portadas se buscan por artista y titulo: si el artista del tag es un
    # canal hay que resolver la identidad desde el nombre del archivo, igual que
    # hace scan, o se consulta con datos inservibles.
    contexto = getattr(args, "contexto", None)
    if contexto is None:
        print("  Revisando artistas dudosos...")
        biblioteca = list(iter_files(args.root))
        categories = configure_folder_categories(biblioteca, args.root)
        if categories:
            print("  carpetas tematicas ignoradas: %s" %
                  ", ".join(sorted(categories, key=str.casefold)))
        cover_suspects = artists_mod.suspect_paths(
            artists_mod.judge(artists_mod.collect(biblioteca)))
        cover_suspects.update(artists_mod.category_impostor_paths(
            biblioteca, args.root, categories))
        print("  artistas no fiables: %d" % len(cover_suspects))
        known = artists_mod.known_names(biblioteca, args.root)
        print("  artistas que la biblioteca ya conoce: %d\n" % len(known))
    else:
        biblioteca = contexto["files"]
        set_category_folders(contexto["categories"])
        cover_suspects = contexto["suspects"]
        known = contexto["known"]

    providers = Providers(CACHE_DIR)
    usar_consenso = getattr(args, "consenso", False)

    embedded, no_match, no_art, igual, identica = 0, 0, 0, 0, 0
    mejoradas = corregidas = 0
    failures, touched, sin_portada, conservadas = [], [], [], []
    total = len(pending)

    # Las que eligio una persona: se ponen tal cual, sin preguntar a nadie.
    for ruta in por_poner:
        datos, error = _portada_declarada(declaradas[ruta])
        if not datos:
            failures.append((str(ruta), "correcciones.json: %s" % error))
            continue
        actual = (read_audio_info(ruta) or {}).get("cover_data") or b""
        if actual != datos:
            ok, error = embed_cover(ruta, datos, covers_mod.guess_mime(datos))
            if not ok:
                failures.append((str(ruta), error))
                continue
            embedded += 1
            touched.append(str(ruta))
        ledger.record(ruta, datos, covers_mod.MANUAL_SOURCE)

    print("  Barra: tiempo usado / tiempo restante aproximado")
    started = time.monotonic()
    processed = 0
    try:
        for index, path in enumerate(pending, start=1):
            progress_bar(processed, total, started, path)

            artist, title, raw = identity_for(path, args.root, cover_suspects,
                                              known=known)
            if not title:
                no_match += 1
                sin_portada.append("%s\n    sin titulo reconocible" % path)
                processed = index
                continue

            info = read_audio_info(path)
            buscar = (providers.consensus_lookup if usar_consenso
                      else providers.lookup)
            match = buscar(artist, title,
                           duration=(info or {}).get("duration") or None,
                           want_date=False,
                           hints=online_artist_hints(
                               raw, folder_hints(path, args.root)))
            if not match:
                no_match += 1
                sin_portada.append(
                    "%s\n    busque: artista=%r titulo=%r\n"
                    "    ninguna fuente la conoce" % (path, artist, title))
                processed = index
                continue

            # En consenso, "revisar" significa literalmente que las fuentes no
            # alcanzaron una verificacion suficiente. Antes aun se incrustaba la
            # portada si el archivo estaba vacio; asi una coincidencia lejana de
            # iTunes podia acabar repetida en canciones ajenas. Alta (acuerdo) y
            # media (una fuente con titulo/artista exactos) si son utilizables.
            if usar_consenso and match.get("confidence") == "revisar":
                no_match += 1
                sin_portada.append(
                    "%s\n    coincidencia dudosa: %r de %r; no se aplico"
                    % (path, match.get("artist", ""), match.get("album", "")))
                processed = index
                continue

            # Si lo que ya hay es una portada correcta, no vale la pena bajar
            # una candidata que la propia URL anuncia como mas pequena: con la
            # regla de "lo que esta bien no se toca" nunca se usaria.
            actual_previa = (info or {}).get("cover_data") or b""
            provisional = (ledger.fuente(path)
                           == covers_mod.ARTIST_PHOTO_SOURCE)
            suelo = 0
            if (actual_previa and not covers_mod.bad_cover(actual_previa)
                    and not provisional
                    and hashlib.sha1(actual_previa).hexdigest() not in repartidas):
                # Una candidata menor que la que ya hay no se usa nunca: ni
                # mejora si es la misma foto, ni gana si es otra. No se baja.
                # Salvo que la que hay este repartida por la carpeta: esa hay
                # que sustituirla aunque la candidata sea mas pequena.
                suelo = covers_mod.min_side(actual_previa)
                # Y una que ya es HD y no puso el programa no la cambia nada:
                # la misma foto mas grande no merece reescribir el archivo, y
                # otra imagen no es suya. No se baja ninguna candidata.
                if (suelo >= covers_mod.LADO_HD
                        and not ledger.placed_by_program(path)):
                    suelo = covers_mod.LADO_PEDIDO
            data = providers.fetch_artwork(match, min_side_wanted=suelo)
            if suelo and not data:
                # Ninguna candidata superaba a la que ya hay: se queda como esta.
                igual += 1
                processed = index
                continue
            if not data or len(data) < covers_mod.MIN_USEFUL_BYTES:
                no_art += 1
                sin_portada.append(
                    "%s\n    la encontraron como %r, pero sin caratula"
                    % (path, match.get("album", "")))
                processed = index
                continue

            # Una sola regla, en covers.decide_cover, para que el programa y
            # las pruebas no puedan discrepar.
            actual = (info or {}).get("cover_data") or b""
            # La foto del artista es un apano: cualquier portada de verdad la
            # sustituye, aunque sea mas pequena. Igual que una repartida.
            compartida = bool(actual) and (
                hashlib.sha1(actual).hexdigest() in repartidas or provisional)
            veredicto = covers_mod.decide_cover(
                actual, data,
                from_program=ledger.placed_by_program(path),
                shared=compartida)

            if veredicto == covers_mod.COVER_SAME:
                identica += 1
                processed = index
                continue
            if veredicto == covers_mod.COVER_KEEP:
                igual += 1
                conservadas.append(
                    "%s\n    actual %s%s; la fuente propone otra imagen (%s) "
                    "de %r via %s"
                    % (path, covers_mod.size_text(actual),
                       "" if ledger.placed_by_program(path)
                       else " (no la puso el programa)",
                       covers_mod.size_text(data), match.get("album", ""),
                       match.get("source", "")))
                processed = index
                continue
            if veredicto == covers_mod.COVER_UPGRADE:
                mejoradas += 1
            elif veredicto == covers_mod.COVER_FIX:
                corregidas += 1

            ok, error = embed_cover(path, data, covers_mod.guess_mime(data))
            if ok:
                embedded += 1
                touched.append(str(path))
                ledger.record(path, data, match.get("source", ""))
            else:
                failures.append((str(path), error))
            processed = index
    except KeyboardInterrupt:
        print("\nInterrumpido. Lo descargado se conserva, y la cache tambien.")
    finally:
        progress_bar(processed, max(total, 1), started,
                     "Terminado" if processed == total else "Interrumpido")
        print("")
        ledger.flush()

    # Segunda pasada: los que siguen sin portada la toman de otra cancion del
    # mismo album, y si no del mismo artista. No es la caratula exacta del tema,
    # pero es la del disco correcto o al menos la del artista correcto, que es
    # mucho mejor que el hueco gris del reproductor.
    heredadas = fotos = 0
    if args.apply:
        no_resueltos = [linea.split("\n")[0] for linea in sin_portada]
        heredadas, hered_fallos, fotos = heredar_portadas(
            files, args.root, cover_suspects, touched, no_resueltos, known,
            ledger=ledger,
            foto_artista=_buscador_de_fotos(providers, biblioteca))
        failures.extend(hered_fallos)
        ledger.flush()
    providers.close()

    log_path = write_log("portadas", {"files": touched}) if touched else None

    print("\n" + "=" * 66)
    print("  Portadas incrustadas   : %d" % embedded)
    print("  Heredadas del artista  : %d" % heredadas)
    print("    de ellas, foto del artista: %d" % fotos)
    print("  Sin coincidencia       : %d" % no_match)
    print("  Encontrado sin caratula: %d" % no_art)
    print("  Se dejo la que ya habia (la fuente proponia otra imagen): %d" % igual)
    print("  Misma imagen, cambiada solo por una de mas resolucion: %d" % mejoradas)
    print("  Corregidas: la que habia puesto el programa era otra imagen: %d"
          % corregidas)
    if getattr(providers, "artwork_skipped", 0):
        print("  Descargas evitadas (la candidata era menor): %d"
              % providers.artwork_skipped)
    print("  Ya tenia exactamente esa portada: %d" % identica)
    print(providers.summary())
    print("=" * 66)
    txt = write_txt(informe_de("sin-portada", args.carpeta, "txt"),
                    "Archivos que siguen sin portada tras consultar las fuentes",
                    sin_portada)
    if txt:
        print("  Sin portada (%d): %s" % (len(sin_portada), txt))
    kept_txt = write_txt(
        informe_de("portada-conservada", args.carpeta, "txt"),
        "Portadas que no se tocaron: o no las puso el programa, o la fuente "
        "proponia una imagen menor. Miralas y decide tu",
        conservadas)
    if kept_txt:
        print("  Conservadas sin tocar (%d): %s" %
              (len(conservadas), kept_txt))
    if log_path:
        print("  Log: %s" % log_path)
        print('  Deshacer: python main.py deshacer "%s"' % log_path)
    show_failures(failures)
    return 0



def heredar_portadas(files, root, suspects, touched, sin_resolver=(),
                     known=None, ledger=None, foto_artista=None):
    """Reparte portadas entre canciones que comparten titulo, album o artista.

    Cuando ninguna fuente conoce un tema pero si conoce otro igual o del mismo
    artista, esa portada sirve. Se prueba en orden de fiabilidad:

      1. mismo titulo, artista (si se conoce) y duracion compatible
                         -> es la misma grabacion aunque un tag este incompleto
      2. mismo album    -> misma caratula por definicion
      3. foto del artista -> cuando no hay ni la del tema ni la del disco
      4. mismo artista  -> no es la del single, pero identifica al artista

    Hay dos clases de donante. El confirmado es el que ademas resolvio online:
    su portada es la del disco de verdad y sustituye incluso a una que ya
    parecia buena. El de reserva solo tiene una imagen cuadrada correcta, y con
    ese se rellenan huecos, pero no se pisa nada.

    El artista se agrupa por el principal y sin acentos, para que "Hns. Yaipen"
    y "Hermanos Yaipen" compartan la misma caratula en vez de quedarse cada uno
    con su hueco gris.
    """
    from collections import defaultdict

    from tagger.deezer import duration_tolerance
    from tagger.filenames import folder_hints
    from tagger.text import main_artist, norm

    if ledger is None:
        ledger = covers_mod.CoverLedger(CACHE_DIR / "portadas.json")

    # Un titulo solo no identifica una grabacion: Sam Smith y Miki Matsubara
    # tienen canciones llamadas "Stay With Me". Para el rescate por titulo se
    # exige tambien una duracion compatible. El album se liga al artista para
    # que dos "Greatest Hits" no compartan portada entre grupos distintos.
    por_titulo, alt_titulo = defaultdict(list), defaultdict(list)
    por_album, por_artista, por_carpeta = {}, {}, {}
    alt_album, alt_artista, alt_carpeta = {}, {}, {}
    faltan = []
    pendientes = {str(x) for x in sin_resolver}

    def portada_mismo_titulo(mapa, clave, duracion, artista):
        if not clave or not duracion:
            return None
        for otra_duracion, otro_artista, imagen in mapa.get(clave, ()):
            if artista and otro_artista and artista != otro_artista:
                continue
            if (otra_duracion and abs(otra_duracion - duracion)
                    <= duration_tolerance(duracion)):
                return imagen
        return None

    for path in files:
        info = read_audio_info(path)
        tags = read_tags(path)
        if info is None or tags is None:
            continue
        data = info.get("cover_data") or b""
        buena = bool(data) and not covers_mod.bad_cover(data)
        resuelto = str(path) not in pendientes
        artista, titulo, _ = identity_for(path, root, suspects, known=known)
        # La carpeta del artista es una clave por si sola: si alguien guardo
        # siete temas en "hermanos yaipen", son del mismo grupo aunque un tag
        # diga "Hns. Yaipen" y otro "Hermanos Yaipen". La carpeta de genero no
        # vale para esto, y folder_hints ya la descarta.
        carpeta = folder_hints(path, root)["artist_folder"]
        artista_resuelto = artista
        if not artista_resuelto and str(path) not in suspects:
            artista_resuelto = tags.get("artist", "")
        c_titulo = norm(titulo or tags.get("title", ""))
        c_artista = norm(main_artist(artista_resuelto))
        c_carpeta = norm(carpeta)
        album_simple = norm(tags.get("album", ""))
        c_album = (album_simple, c_artista) \
            if album_simple and c_artista else ()
        duracion = float(info.get("duration") or 0)
        claves = (c_titulo, c_album, c_artista, c_carpeta)

        if buena:
            if c_titulo and duracion:
                (por_titulo if resuelto else alt_titulo)[c_titulo].append(
                    (duracion, c_artista, data))
            destino = ((por_album, por_artista, por_carpeta) if resuelto
                       else (alt_album, alt_artista, alt_carpeta))
            for mapa, clave in zip(destino, claves[1:]):
                if clave:
                    mapa.setdefault(clave, data)
        # Una portada elegida a mano en correcciones.json no entra en el
        # reparto: la decidio una persona y ninguna herencia la mejora. Sin
        # esto, bastaba con que el detector de bandas se equivocara con una
        # caratula muy oscura para que el ultimo recurso ("otro disco del
        # mismo artista") le pusiera encima la de otro tema.
        if ((not buena or not resuelto)
                and ledger.fuente(path) != covers_mod.MANUAL_SOURCE):
            faltan.append((path, claves, duracion, data, buena,
                           main_artist(artista_resuelto)))

    puestas, fotos, fallos = 0, 0, []
    for (path, (c_titulo, c_album, c_artista, c_carpeta), duracion,
         actual, buena, nombre_artista) in faltan:
        # Donante confirmado por titulo o por album: es la caratula de ese
        # mismo disco, no un parecido.
        data = (portada_mismo_titulo(
                    por_titulo, c_titulo, duracion, c_artista)
                or por_album.get(c_album))
        confirmado = data is not None
        fuente = "heredada de otro tema"
        if data is None:
            data = (portada_mismo_titulo(
                        alt_titulo, c_titulo, duracion, c_artista)
                    or alt_album.get(c_album))
        # Ni la del tema ni la del disco: antes que la caratula de OTRO disco
        # del mismo artista, su foto.
        if data is None and not buena and foto_artista and nombre_artista:
            data = foto_artista(nombre_artista)
            if data:
                fuente = covers_mod.ARTIST_PHOTO_SOURCE
        if data is None:
            data = (por_artista.get(c_artista) or alt_artista.get(c_artista)
                    or por_carpeta.get(c_carpeta) or alt_carpeta.get(c_carpeta))
        if not data or data == actual:
            continue
        # Una portada que ya parece correcta no se cambia por otra imagen,
        # venga de donde venga. Solo se acepta la misma foto con mas pixeles.
        # Antes, un donante "confirmado" podia sustituirla, y bastaba con que
        # el donante tuviera el titulo o el album parecido para colar una
        # caratula distinta encima de una que estaba bien.
        if buena:
            if not covers_mod.same_picture(actual, data):
                continue
            if covers_mod.min_side(data) <= covers_mod.min_side(actual):
                continue
        ok, error = embed_cover(path, data, covers_mod.guess_mime(data))
        if ok:
            puestas += 1
            touched.append(str(path))
            # Queda constancia de que esta la puso el programa copiandola de
            # otro tema. Sin esto, la siguiente pasada la toma por tuya y no se
            # atreve a corregirla: es como se quedaron 52 canciones de brasil
            # con la misma imagen.
            ledger.record(path, data, fuente)
            if fuente == covers_mod.ARTIST_PHOTO_SOURCE:
                fotos += 1
        else:
            fallos.append((str(path), error))
    return puestas, fallos, fotos


# ---------------------------------------------------------------------------
# artistas
# ---------------------------------------------------------------------------

def run_artists(args):
    files = list(iter_files(args.root, args.carpeta, args.limit))
    total = len(files)
    print("Leyendo %d archivos..." % total)
    findings = artists_mod.judge(
        artists_mod.collect(files, progress=lambda i: progress(i, total)))
    # Las fichas revisadas a mano no se juzgan: ni se corrigen ni se mandan a
    # revisar otra vez.
    verificadas = VerificationLedger(CACHE_DIR / "verificadas.json")
    findings = [f for f in findings
                if not verificadas.is_manual(f["path"], read_tags(f["path"]))]

    orden = {"formato": 0, "canal": 1, "dudoso": 2}
    findings.sort(key=lambda f: (orden[f["verdict"]], str(f["path"])))

    rows = [[f["verdict"], str(f["path"]), f["current"], f["suggested"], f["reason"]]
            for f in findings]
    csv_path = write_csv(args.csv, ["veredicto", "archivo", "artista_actual",
                                    "artista_sugerido", "motivo"], rows)

    counts = Counter(f["verdict"] for f in findings)
    print("\n" + "=" * 66)
    print("  Artista puesto pero sospechoso: %d" % len(findings))
    print("    formato  %4d  mismo nombre peor escrito (se corrige solo)"
          % counts["formato"])
    print("    canal    %4d  nombre de canal o sello en vez del artista"
          % counts["canal"])
    print("    dudoso   %4d  el tag no cuadra con el nombre del archivo"
          % counts["dudoso"])
    print("=" * 66)

    for verdict in ("formato", "canal", "dudoso"):
        muestra = [f for f in findings if f["verdict"] == verdict][:3]
        if not muestra:
            continue
        print("\n  %s:" % verdict.upper())
        for f in muestra:
            print("    %s" % f["path"].name[:62])
            print("      %r -> %r" % (f["current"], f["suggested"]))

    if csv_path:
        print("\n  Informe: %s" % csv_path)

    if not args.apply:
        print("\n  Nada escrito. Con --apply se corrigen SOLO los de veredicto")
        print("  'formato'; 'canal' y 'dudoso' se quedan para que los mires tu.")
        return 0

    backup = Backup(BACKUP_DIR)
    written, failures = 0, []
    for f in findings:
        if f["verdict"] != "formato":
            continue
        original = read_tags(f["path"])
        if original is None:
            continue
        backup.record(f["path"], {k: original.get(k, "") for k in FIELDS})
        ok, error = write_tags(f["path"], {"artist": f["suggested"]})
        if ok:
            written += 1
        else:
            failures.append((str(f["path"]), error))
    backup_path = backup.save()

    print("\n  Artistas corregidos: %d" % written)
    if backup_path:
        print("  Backup: %s" % backup_path)
        print('  Deshacer: python main.py restaurar "%s"' % backup_path)
    show_failures(failures)
    return 0


# ---------------------------------------------------------------------------
# generos
# ---------------------------------------------------------------------------

def run_genres(args):
    files = list(iter_files(args.root, args.carpeta, args.limit))
    total = len(files)
    rows, sin_genero = [], 0

    for index, path in enumerate(files, start=1):
        progress(index, total)
        tags = read_tags(path)
        if tags is None:
            continue
        result = genre_check(path, args.root, tags.get("genre", ""))
        if result is None:
            if not tags.get("genre"):
                sin_genero += 1
            continue
        rows.append([str(path), result["folder"], result["genre"],
                     result["canonical"], result["suggested"]])

    csv_path = write_csv(args.csv, ["archivo", "carpeta_actual", "genero_tag",
                                    "genero_normalizado", "carpeta_sugerida"], rows)

    print("\n" + "=" * 66)
    print("  Archivos revisados        : %d" % total)
    print("  Posiblemente mal ubicados : %d" % len(rows))
    print("  Sin genero que contrastar : %d" % sin_genero)
    print("=" * 66)
    counter = Counter((row[1], row[4]) for row in rows)
    for (folder, suggested), count in counter.most_common(12):
        print("    %-14s -> %-14s %4d" % (folder, suggested or "?", count))
    if csv_path:
        print("\n  Informe: %s" % csv_path)
    print("  Solo informa: ningun archivo se ha movido.")
    return 0


# ---------------------------------------------------------------------------
# compatibilidad
# ---------------------------------------------------------------------------

def run_compat(args):
    """Pasa las etiquetas a ID3v2.3, que es lo que leen todos los reproductores.

    Mutagen escribe v2.4 por defecto, y Windows Media Player, Groove, muchos
    equipos de coche y los Android antiguos no lo entienden: ven el archivo pero
    ignoran las etiquetas. Es la causa tipica de "he cambiado los tags y el
    reproductor sigue mostrando lo de antes".
    """
    files = list(iter_files(args.root, args.carpeta, args.limit))
    total = len(files)

    # Antes que la version de la etiqueta, el contenedor: un MP4 con extension
    # .mp3 no muestra ni datos ni portada por mucho que se le escriban.
    vacios = [p for p in files if conversion.esta_vacio(p)]
    disfrazados = [p for p in files if conversion.es_mp4_disfrazado(p)]
    if vacios:
        print("  Vacios (0 bytes, sin audio; hay que volver a bajarlos): %d"
              % len(vacios))
        for path in vacios[:8]:
            print("    %s" % path)
    if disfrazados:
        print("  MP4 con extension .mp3 (sin datos visibles): %d" % len(disfrazados))
        if not args.apply:
            for path in disfrazados[:8]:
                print("    %s" % path.name)
            print("  Nada convertido. Con --apply se pasan a MP3 de verdad.")
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            copias = BACKUP_DIR / ("convertidos-%s" % stamp)
            hechos, fallos_conv = [], []
            for index, path in enumerate(disfrazados, start=1):
                progress(index, len(disfrazados))
                copia, error = conversion.convertir(path, copias)
                if copia:
                    hechos.append({"archivo": str(path), "copia": str(copia)})
                else:
                    fallos_conv.append((str(path), error))
            print("\n  Convertidos a MP3: %d" % len(hechos))
            if hechos:
                log_path = write_log("convertidos", {"files": hechos})
                print('  Deshacer: python main.py deshacer "%s"' % log_path)
            show_failures(fallos_conv)

    print("Revisando la version de etiqueta de %d archivos..." % total)

    viejos = []
    for index, path in enumerate(files, start=1):
        progress(index, total)
        version = id3_version(path)
        if version and version >= (2, 4):
            viejos.append(path)

    print("\n" + "=" * 66)
    print("  En ID3v2.4 (no todos lo leen): %d" % len(viejos))
    print("  Ya compatibles               : %d" % (total - len(viejos)))
    print("=" * 66)

    if not viejos:
        print("  Nada que convertir.")
        return 0
    if not args.apply:
        for path in viejos[:8]:
            print("    %s" % path.name)
        if len(viejos) > 8:
            print("    ... y %d mas" % (len(viejos) - 8))
        print("\n  Nada modificado. Repite con --apply para convertirlos.")
        return 0

    backup = Backup(BACKUP_DIR)
    convertidos, failures = 0, []
    for index, path in enumerate(viejos, start=1):
        progress(index, len(viejos))
        original = read_tags(path)
        if original is not None:
            backup.record(path, {f: original.get(f, "") for f in FIELDS})
        ok, error = downgrade_to_v23(path)
        if ok:
            convertidos += 1
        else:
            failures.append((str(path), error))
    backup_path = backup.save()

    print("\n  Convertidos a ID3v2.3: %d" % convertidos)
    if backup_path:
        print("  Backup: %s" % backup_path)
        print('  Deshacer: python main.py restaurar "%s"' % backup_path)
    show_failures(failures)
    return 0


# ---------------------------------------------------------------------------
# todo
# ---------------------------------------------------------------------------

def carpetas_de_primer_nivel(root):
    """Carpetas de primer nivel en orden alfabetico, y si hay sueltos, la raiz.

    Se salta "_duplicados", que es la cuarentena adonde van los repetidos.
    """
    try:
        nombres = sorted((x.name for x in root.iterdir()
                          if x.is_dir() and x.name != "_duplicados"),
                         key=str.casefold)
    except OSError:
        return []
    # Archivos sueltos en la raiz: no tienen carpeta que los recoja, asi que se
    # procesan aparte al final.
    sueltos = any(x.is_file() and x.suffix.lower() in (".mp3", ".m4a", ".flac")
                  for x in root.iterdir())
    return nombres + ([""] if sueltos else [])


def _cadena(args, carpeta, contexto):
    """Los cinco pasos sobre una carpeta. Devuelve el codigo de salida."""
    pasos = [
        ("1/5  Nombres de archivo", run_names,
         {"csv": informe_de("nombres", carpeta, "csv")}),
        ("2/5  Artistas mal escritos", run_artists,
         {"csv": informe_de("artistas", carpeta, "csv")}),
        ("3/5  Compatibilidad ID3v2.3", run_compat, {}),
        ("4/5  Tags: titulo, artista, album, fecha y genero", run_scan,
         {"online": True, "consenso": True, "verificar": True,
          "include_doubtful": False, "min_score": 90,
          "no_revisar_artistas": False, "contexto": contexto,
          "csv": informe_de("informe", carpeta, "csv")}),
        ("5/5  Portadas", run_covers,
         {"consenso": True, "forzar": not args.respetar_portadas,
          "contexto": contexto}),
    ]

    for titulo, funcion, extra in pasos:
        print("")
        print("#" * 70)
        print("#  %s" % titulo)
        print("#" * 70)
        paso = argparse.Namespace(root=args.root, carpeta=carpeta,
                                  limit=args.limit, apply=args.apply)
        for clave, valor in extra.items():
            setattr(paso, clave, valor)
        # Los artistas no fiables se apuntan por ruta. Si el paso 1 renombro
        # algun archivo, su marca se quedo en la ruta vieja y el paso 4 lo
        # trataba como fiable: "ATARASHII GAKKO! - Oi AG! .mp3" perdio asi la
        # suya por un espacio.
        if (funcion is run_scan
                and any(not ruta.exists() for ruta in contexto["files"])):
            contexto.update(contexto_biblioteca(args.root, mostrar=False))
        codigo = funcion(paso)
        if codigo:
            return codigo
    return 0


def run_todo(args):
    """Toda la cadena en un solo comando, en el orden que importa.

    Cada paso mejora los datos que usa el siguiente: los nombres limpios ayudan
    a identificar al artista, el artista correcto permite separar el titulo, y
    con artista y titulo buenos las fuentes online aciertan mucho mas. Las
    portadas van al final, cuando ya se sabe de que cancion se trata.

    Sin --carpeta se recorre la biblioteca entera, pero una carpeta de primer
    nivel por turno y en orden alfabetico: asi se ve por donde va, cada carpeta
    deja su propio informe, y cortar con Ctrl+C conserva lo hecho hasta la
    ultima terminada. Todo dentro del mismo proceso, para no rehacer las
    lecturas ni el contexto global en cada una.
    """
    if args.carpeta:
        contexto = contexto_biblioteca(args.root)
        codigo = _cadena(args, args.carpeta, contexto)
        if codigo:
            print("\n  El paso fallo. Se detiene aqui.")
            return codigo
        _despedida(args, args.carpeta)
        return 0

    carpetas = carpetas_de_primer_nivel(args.root)
    if not carpetas:
        print("No hay carpetas que procesar en %s" % args.root)
        return 1

    print("Carpetas a procesar, en este orden: %s"
          % ", ".join(x or "(sueltos en la raiz)" for x in carpetas))

    hechas, fallos = 0, []
    for indice, carpeta in enumerate(carpetas, start=1):
        print("")
        print("#" * 70)
        print("#  CARPETA %d/%d: %s"
              % (indice, len(carpetas), carpeta or "(sueltos en la raiz)"))
        print("#" * 70)
        # El contexto se rehace por carpeta: los pasos anteriores han escrito
        # tags y renombrado archivos, y el vocabulario de artistas ya no es el
        # mismo. Cuesta unos segundos y evita trabajar con datos viejos.
        arranque = time.monotonic()
        try:
            contexto = contexto_biblioteca(args.root)
            codigo = _cadena(args, carpeta, contexto)
        except KeyboardInterrupt:
            print("\n  Interrumpido. Lo hecho se conserva.")
            return 1
        except Exception:
            # Un MP3 corrupto, un disco que falla, una respuesta rara de una
            # fuente: lo que sea. Antes esto mataba las carpetas que faltaban,
            # que es justo lo que el BAT evitaba cuando el bucle estaba fuera.
            print("\n  *** %s fallo con una excepcion. Se sigue con la "
                  "siguiente." % (carpeta or "raiz"))
            traceback.print_exc()
            hechas += 1
            fallos.append(carpeta or "raiz")
            continue
        hechas += 1
        print("\n  %s: %s" % (carpeta or "raiz",
                              _short_time(time.monotonic() - arranque)))
        if codigo:
            print("\n  *** %s termino con error %d. Se sigue con la siguiente."
                  % (carpeta or "raiz", codigo))
            fallos.append(carpeta or "raiz")

    print("")
    print("=" * 70)
    print("  TERMINADO. Carpetas procesadas: %d de %d" % (hechas, len(carpetas)))
    if fallos:
        print("  Carpetas con error: %s" % ", ".join(fallos))
    print("=" * 70)
    _despedida(args, "")
    return 1 if fallos else 0


def _despedida(args, carpeta):
    if carpeta:
        print("")
        print("=" * 70)
        print("  TERMINADO: %s" % carpeta)
        print("=" * 70)
    if args.apply:
        print("  Todo reversible:")
        print("    tags     -> python main.py restaurar backups/<el mas reciente>.json")
        print("    nombres  -> python main.py deshacer  logs/renombrados/renombrados-*.json")
        print("    portadas -> python main.py deshacer  logs/portadas/portadas-*.json")
        print("")
        print("  Ahora dile a tu reproductor que vuelva a leer la biblioteca.")
    else:
        print("  Era solo informe. Repite con --apply para escribir.")


# ---------------------------------------------------------------------------
# restaurar / deshacer
# ---------------------------------------------------------------------------

def run_fijar(args):
    """Da por buenas las fichas revisadas a mano.

    Mientras sus tags sigan como estan, el programa no les cambia nada: ni las
    fuentes ni la limpieza local. Si alguien cambia un tag, la marca deja de
    valer sola. Es lo mismo que "elegida a mano" en las portadas, pero para
    artista, titulo, album, fecha y genero.

    Sin filtros se fija todo lo que haya en --carpeta. Con --desde, solo lo
    modificado despues de esa fecha; con --elegidas-a-mano, lo que tiene la
    portada elegida a mano. Con los dos, vale cualquiera de ellos.
    """
    files = list(iter_files(args.root, args.carpeta, args.limit))
    desde = None
    if args.desde:
        try:
            desde = datetime.strptime(args.desde, "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            print('ERROR: --desde tiene que ser "AAAA-MM-DD HH:MM".')
            return 1
    ledger = (covers_mod.CoverLedger(CACHE_DIR / "portadas.json")
              if args.elegidas_a_mano else None)

    elegidos = []
    for ruta in files:
        if desde is None and ledger is None:
            elegidos.append(ruta)
        elif desde is not None and ruta.stat().st_mtime > desde:
            elegidos.append(ruta)
        elif (ledger is not None
              and ledger.fuente(ruta) == covers_mod.MANUAL_SOURCE):
            elegidos.append(ruta)

    verificadas = VerificationLedger(CACHE_DIR / "verificadas.json")
    ya = sum(1 for ruta in elegidos
             if verificadas.is_manual(ruta, read_tags(ruta)))
    print("\n  Archivos revisados : %d" % len(files))
    print("  Elegidos           : %d" % len(elegidos))
    print("  Ya estaban fijados : %d" % ya)
    for ruta in elegidos[:8]:
        print("    %s" % ruta.name)
    if len(elegidos) > 8:
        print("    ... y %d mas" % (len(elegidos) - 8))
    if not args.apply:
        print("\n  Nada %s. Repite con --apply."
              % ("soltado" if args.soltar else "fijado"))
        return 0

    cambiados = 0
    for ruta in elegidos:
        if args.soltar:
            entrada = verificadas.data.get(str(ruta))
            if isinstance(entrada, dict) and entrada.get("source") == FIJADA:
                verificadas.unmark_manual(ruta)
                cambiados += 1
            continue
        tags = read_tags(ruta)
        if tags is None:
            continue
        verificadas.mark_manual(ruta, tags)
        cambiados += 1
    verificadas.flush()
    print("\n  %s: %d" % ("Soltadas" if args.soltar else "Fijadas", cambiados))
    if not args.soltar:
        print("  Para soltar: python main.py fijar --carpeta X --soltar --apply")
    return 0


def run_restore(args):
    if not args.backup.exists():
        print("ERROR: no existe %s" % args.backup)
        return 1
    restored, failures = restore(args.backup)
    print("Tags restaurados: %d" % restored)
    show_failures(failures)
    return 0


def run_undo(args):
    if not args.log.exists():
        print("ERROR: no existe %s" % args.log)
        return 1
    with open(args.log, encoding="utf-8") as handle:
        payload = json.load(handle)

    kind = payload.get("kind")
    if kind == "portadas":
        removed, failures = 0, []
        for raw in payload.get("files", []):
            path = Path(raw)
            if not path.exists():
                failures.append((raw, "no existe"))
                continue
            ok, error = remove_cover(path)
            if ok:
                removed += 1
            else:
                failures.append((raw, error))
        print("Portadas retiradas: %d" % removed)
    elif kind in ("move", "rename"):
        mover, guardar = _al_renombrar()
        restored, failures = undo(args.log, on_rename=mover)
        guardar()
        print("Devueltos a su sitio: %d" % restored)
    elif kind == "convertidos":
        # El archivo vuelve a ser el original, tal como estaba antes de
        # convertirlo a MP3.
        restored, failures = 0, []
        for entrada in payload.get("files", []):
            copia, destino = Path(entrada["copia"]), Path(entrada["archivo"])
            if not copia.exists():
                failures.append((str(copia), "no existe la copia"))
                continue
            try:
                shutil.copy2(copia, destino)
                restored += 1
            except OSError as error:
                failures.append((str(destino), str(error)))
        print("Originales devueltos: %d" % restored)
    else:
        print("ERROR: log de tipo desconocido (%r)." % kind)
        print("       Para backups de tags usa: python main.py restaurar")
        return 1

    show_failures(failures)
    return 0


# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py", description="Limpia y completa los metadatos de tu musica.")
    sub = parser.add_subparsers(dest="command")

    todo = sub.add_parser("todo", help="TODO en un solo comando (lo habitual)")
    add_common(todo)
    todo.add_argument("--apply", action="store_true", help="escribe de verdad")
    todo.add_argument("--respetar-portadas", action="store_true",
                      help="no toca las portadas que ya existen (por defecto se "
                           "cambian por la del album real, salvo que la tuya sea "
                           "de mas resolucion)")

    scan = sub.add_parser("scan", help="limpia y completa los tags")
    add_common(scan)
    scan.add_argument("--online", action="store_true", help="consulta las fuentes")
    scan.add_argument("--apply", action="store_true", help="escribe los cambios")
    scan.add_argument("--incluir-dudosos", dest="include_doubtful",
                      action="store_true",
                      help="escribe tambien lo marcado 'revisar' o 'baja'")
    scan.add_argument("--verificar", action="store_true",
                      help="comprueba tambien los albumes que ya estan puestos")
    scan.add_argument("--consenso", action="store_true",
                      help="compara las fuentes y exige acuerdo o verificacion fuerte")
    scan.add_argument("--no-revisar-artistas", action="store_true",
                      help="salta la deteccion previa de artistas dudosos")
    scan.add_argument("--min-score", type=int, default=90)
    scan.add_argument("--csv", type=Path, default=REPORT_DIR / "general" / "informe.csv")

    dup = sub.add_parser("duplicados", help="encuentra canciones repetidas")
    add_common(dup)
    dup.add_argument("--apply", action="store_true",
                     help="mueve los sobrantes a _duplicados (no borra)")
    dup.add_argument("--csv", type=Path, default=REPORT_DIR / "general" / "duplicados.csv")

    names = sub.add_parser("nombres", help="quita emojis y rarezas del nombre")
    add_common(names)
    names.add_argument("--apply", action="store_true", help="renombra de verdad")
    names.add_argument("--csv", type=Path, default=REPORT_DIR / "general" / "nombres.csv")

    cov = sub.add_parser("portadas", help="descarga caratulas")
    add_common(cov)
    cov.add_argument("--apply", action="store_true", help="descarga e incrusta")
    cov.add_argument("--consenso", action="store_true",
                     help="compara las fuentes antes de elegir la portada")
    cov.add_argument("--forzar", action="store_true",
                     help="reemplaza tambien las portadas que ya existen")
    cov.add_argument("--rehacer", action="store_true",
                     help="vuelve a bajar hasta las portadas que ya puso el "
                          "programa (por defecto se dan por hechas)")

    art = sub.add_parser("artistas", help="artistas rellenos pero mal")
    add_common(art)
    art.add_argument("--apply", action="store_true",
                     help="corrige solo los de veredicto 'formato'")
    art.add_argument("--csv", type=Path, default=REPORT_DIR / "general" / "artistas.csv")

    gen = sub.add_parser("generos", help="avisa de canciones en la carpeta equivocada")
    add_common(gen)
    gen.add_argument("--csv", type=Path, default=REPORT_DIR / "general" / "generos.csv")

    compat = sub.add_parser("compatibilidad",
                            help="pasa las etiquetas a ID3v2.3 (lo que leen todos)")
    add_common(compat)
    compat.add_argument("--apply", action="store_true", help="convierte de verdad")

    fij = sub.add_parser("fijar",
                         help="da por buenas las fichas revisadas a mano")
    add_common(fij)
    fij.add_argument("--desde",
                     help='solo los modificados despues de "AAAA-MM-DD HH:MM"')
    fij.add_argument("--elegidas-a-mano", dest="elegidas_a_mano",
                     action="store_true",
                     help="solo los que tienen la portada elegida a mano")
    fij.add_argument("--soltar", action="store_true",
                     help="quita la marca en vez de ponerla")
    fij.add_argument("--apply", action="store_true", help="guarda las marcas")

    rest = sub.add_parser("restaurar", help="deshace una escritura de tags")
    rest.add_argument("backup", type=Path)

    und = sub.add_parser("deshacer", help="deshace movimientos, renombrados o portadas")
    und.add_argument("log", type=Path)

    return parser


COMMANDS = {
    "todo": run_todo,
    "scan": run_scan,
    "duplicados": run_duplicates,
    "nombres": run_names,
    "portadas": run_covers,
    "artistas": run_artists,
    "generos": run_genres,
    "compatibilidad": run_compat,
    "fijar": run_fijar,
    "restaurar": run_restore,
    "deshacer": run_undo,
}


def main():
    setup_console()
    parser = build_parser()
    args = parser.parse_args()
    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    if hasattr(args, "root") and not args.root.exists():
        print("ERROR: la carpeta %s no existe." % args.root)
        return 1
    # Solo uno escribiendo a la vez. Los informes (sin --apply) no tocan la
    # biblioteca y pueden correr a la par.
    if not (getattr(args, "apply", False)
            or args.command in ("restaurar", "deshacer")):
        return handler(args)
    candado = Candado(CACHE_DIR / "en-uso.lock")
    try:
        candado.adquirir()
    except BibliotecaOcupada:
        print("ERROR: ya hay otro proceso escribiendo en la biblioteca (otro "
              "MUSICA.bat o main.py). Espera a que termine y vuelve a lanzarlo.")
        return 1
    try:
        return handler(args)
    finally:
        candado.soltar()


if __name__ == "__main__":
    sys.exit(main())

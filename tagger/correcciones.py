"""Correcciones a mano: lo que una persona decidio sobre un archivo concreto.

correcciones.json guarda, por archivo, lo que no se puede deducir o se decidio
mirandolo: artist, title, album, albumartist, date, genre y portada (una URL o
la ruta de una imagen). El programa lo aplica con sus copias de seguridad y no
deja que ninguna fuente lo cambie. Asi un arreglo a mano es una linea de JSON y
no un script suelto.

La clave es el nombre del archivo ("Eva.mp3") o, cuando ese nombre se repite en
otra carpeta, la ruta dentro de la biblioteca ("latino\\brasil\\Eva.mp3"). Si el
programa renombra el archivo, su correccion va con el.
"""

import json
import os
import threading
from pathlib import Path

CAMPOS = ("artist", "title", "album", "albumartist", "date", "genre", "portada")
CAMPOS_DE_TAG = CAMPOS[:-1]


def _forma(texto):
    return str(texto).replace("\\", "/").strip("/").casefold()


class Correcciones:
    def __init__(self, path):
        self.path = Path(path)
        self._cerrojo = threading.Lock()
        self._sucio = False
        self._cargar()

    def _estado(self):
        try:
            estado = self.path.stat()
        except OSError:
            return None
        return (estado.st_mtime_ns, estado.st_size)

    def _cargar(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                crudo = json.load(handle)
        except (OSError, ValueError):
            crudo = {}
        self.datos = {clave: valor for clave, valor in crudo.items()
                      if isinstance(valor, dict)}
        self._indexar()
        self._visto = self._estado()

    def _indexar(self):
        por_nombre = {}
        for clave in self.datos:
            por_nombre.setdefault(_forma(clave).rsplit("/", 1)[-1], []).append(clave)
        self._por_nombre = por_nombre

    def al_dia(self):
        """Relee el archivo si alguien lo cambio desde fuera."""
        if not self._sucio and self._estado() != self._visto:
            with self._cerrojo:
                if self._estado() != self._visto:
                    self._cargar()
        return self

    def _clave_de(self, path):
        completa = "/" + _forma(path)
        candidatas = [clave for clave in
                      self._por_nombre.get(_forma(Path(path).name), ())
                      if completa.endswith("/" + _forma(clave))]
        # Una ruta dentro de la biblioteca es mas precisa que un nombre suelto.
        return max(candidatas, key=lambda c: len(_forma(c)), default=None)

    def para(self, path):
        """Los campos decididos a mano para este archivo, o {}."""
        clave = self._clave_de(path)
        if clave is None:
            return {}
        return {campo: str(valor).strip()
                for campo, valor in self.datos[clave].items()
                if campo in CAMPOS and str(valor or "").strip()}

    def mover(self, origen, destino):
        """El archivo se renombro: su correccion va con el."""
        with self._cerrojo:
            clave = self._clave_de(origen)
            if clave is None:
                return False
            separador = "\\" if "\\" in clave else "/"
            partes = str(clave).replace("\\", "/").split("/")
            partes[-1] = Path(destino).name
            self.datos[separador.join(partes)] = self.datos.pop(clave)
            self._indexar()
            self._sucio = True
            return True

    def guardar(self):
        if not self._sucio:
            return
        tmp = self.path.with_name("%s.%d.tmp" % (self.path.name, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.datos, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp.replace(self.path)
        self._visto = self._estado()
        self._sucio = False


_COMPARTIDAS = {}
_CERROJO = threading.Lock()


def compartidas(path):
    """Una sola instancia por archivo de correcciones para todo el proceso."""
    clave = str(Path(path).resolve())
    with _CERROJO:
        instancia = _COMPARTIDAS.get(clave)
        if instancia is None:
            instancia = _COMPARTIDAS[clave] = Correcciones(path)
    return instancia.al_dia()

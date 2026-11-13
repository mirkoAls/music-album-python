"""Un solo proceso escribiendo en la biblioteca a la vez.

Dos escrituras simultaneas se pisan: el registro de portadas se carga al
arrancar y se guarda al final, asi que el ultimo en guardar borraba lo que
habia apuntado el otro. Asi se perdieron 39 portadas elegidas a mano cuando
MUSICA.bat corrio mientras se arreglaban otras.

Es un bloqueo del sistema sobre un archivo, no un archivo que se crea y se
borra: si el proceso muere de golpe, Windows lo suelta solo y no queda un
candado huerfano que haya que quitar a mano.
"""

from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None
    import fcntl


class BibliotecaOcupada(RuntimeError):
    """Otro proceso tiene el candado."""


class Candado:
    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def adquirir(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if msvcrt:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise BibliotecaOcupada(str(self.path))
        self._handle = handle

    def soltar(self):
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if msvcrt:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None

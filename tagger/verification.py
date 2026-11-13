"""Registro incremental de canciones ya verificadas contra las fuentes.

La firma usa solo el nombre y los campos musicales. Incrustar una portada cambia
el tamano y la fecha del archivo, pero no debe obligar a consultar otra vez todos
los servicios. Si cambia cualquier tag relevante, el nombre del archivo o la
version de las reglas, la entrada deja de ser valida automaticamente.

Las fichas revisadas a mano ("a mano") van aparte: mientras sus tags sigan
siendo los que se revisaron, el programa no les cambia nada, ni siquiera la
limpieza local, y no caducan al subir la version de las reglas. Es lo mismo que
"elegida a mano" en las portadas.
"""

import hashlib
import json
from pathlib import Path


VERIFICATION_VERSION = 2
VERIFIED_FIELDS = (
    "artist", "title", "album", "albumartist", "date", "genre",
    "tracknumber",
)
REQUIRED_FIELDS = ("artist", "title", "album", "date", "genre")
MANUAL = "a mano"


def tag_signature(path, tags):
    payload = {
        "name": Path(path).name,
        "tags": {field: str((tags or {}).get(field) or "")
                 for field in VERIFIED_FIELDS},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class VerificationLedger:
    """Cache pequena y versionada de verificaciones online fuertes."""

    # Se vuelca cada pocos registros, no solo al terminar: una pasada dura
    # horas y si el proceso muere de golpe sin ese volcado hay que volver a
    # preguntar por todo lo de esa carpeta.
    FLUSH_EVERY = 50

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.dirty = False
        self.pending = 0
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self.data = loaded
            except (ValueError, OSError):
                pass

    @staticmethod
    def complete(tags):
        return all(str((tags or {}).get(field) or "").strip()
                   for field in REQUIRED_FIELDS)

    def is_manual(self, path, tags):
        """Revisada a mano y sin tocar desde entonces."""
        entry = self.data.get(str(path))
        return (isinstance(entry, dict) and entry.get("source") == MANUAL
                and entry.get("signature") == tag_signature(path, tags))

    def is_verified(self, path, tags):
        # Lo revisado a mano no caduca al subir la version: no lo decidio una
        # regla que haya cambiado, lo decidio una persona.
        if self.is_manual(path, tags):
            return True
        entry = self.data.get(str(path))
        if not isinstance(entry, dict):
            return False
        if entry.get("version") != VERIFICATION_VERSION:
            return False
        if not self.complete(tags):
            return False
        return entry.get("signature") == tag_signature(path, tags)

    def record(self, path, tags, source=""):
        if not self.complete(tags):
            return False
        self.data[str(path)] = {
            "version": VERIFICATION_VERSION,
            "signature": tag_signature(path, tags),
            "source": source or "online",
        }
        self._anotar()
        return True

    def mark_manual(self, path, tags):
        """Fija la ficha tal como esta. No exige que este completa: si le falta
        el album es porque no existe, y eso tambien lo decidio quien la reviso."""
        self.data[str(path)] = {
            "version": VERIFICATION_VERSION,
            "signature": tag_signature(path, tags),
            "source": MANUAL,
        }
        self._anotar()

    def unmark_manual(self, path):
        entry = self.data.get(str(path))
        if isinstance(entry, dict) and entry.get("source") == MANUAL:
            del self.data[str(path)]
            self._anotar()

    def move(self, origen, destino, tags):
        """El archivo se renombro. La firma lleva el nombre, asi que se rehace,
        pero solo si los tags siguen siendo los que se verificaron."""
        entry = self.data.pop(str(origen), None)
        if not isinstance(entry, dict):
            return
        if entry.get("signature") == tag_signature(origen, tags):
            self.data[str(destino)] = dict(
                entry, signature=tag_signature(destino, tags))
        self._anotar()

    def _anotar(self):
        self.dirty = True
        self.pending += 1
        if self.pending >= self.FLUSH_EVERY:
            self.flush()

    def flush(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
        temporary.replace(self.path)
        self.dirty = False
        self.pending = 0

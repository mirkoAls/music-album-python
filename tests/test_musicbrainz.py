"""Prueba del cliente de MusicBrainz sin tocar la red."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tagger import musicbrainz as mb


def recording(title, artist, score, album="Un Album", date="2019-05-01",
              secondary=None, primary="Album"):
    return {
        "id": "abc-123",
        "title": title,
        "ext:score": str(score),
        "artist-credit": [{"artist": {"name": artist}}],
        "release-list": [{
            "title": album,
            "date": date,
            "release-group": {
                "primary-type": primary,
                "secondary-type-list": secondary or [],
            },
        }],
    }


class FakeMB:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def search_recordings(self, artist=None, recording=None, limit=None):
        self.calls += 1
        return self.response


def build(response, **kwargs):
    fake = FakeMB(response)
    mb.musicbrainzngs.search_recordings = fake.search_recordings
    tmp = Path(tempfile.mkdtemp()) / "cache.json"
    client = mb.MusicBrainzClient(cache_path=tmp, **kwargs)
    return client, fake


passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  OK   %s" % name)
    else:
        failed += 1
        print("  FALLO %s  %s" % (name, detail))


print("=== coincidencia buena ===")
client, fake = build({"recording-list": [recording("Roller", "Apache 207", 100)]})
match = client.lookup("Apache 207", "Roller")
check("acepta score 100", match is not None and match["confidence"] == "alta", match)
check("extrae album", match and match["album"] == "Un Album")
check("normaliza fecha", match and match["date"] == "2019-05-01", match)

print("\n=== score bajo se rechaza ===")
client, _ = build({"recording-list": [recording("Roller", "Apache 207", 40)]})
check("score 40 -> None", client.lookup("Apache 207", "Roller") is None)

print("\n=== score intermedio se marca para revisar ===")
client, _ = build({"recording-list": [recording("Roller", "Apache 207", 82)]})
match = client.lookup("Apache 207", "Roller")
check("score 82 -> revisar", match and match["confidence"] == "revisar", match)

print("\n=== verificacion local rechaza al impostor ===")
client, _ = build({"recording-list": [recording("Cancion Totalmente Distinta", "Otro Artista", 99)]})
check("titulo y artista no cuadran -> None", client.lookup("Apache 207", "Roller") is None)

print("\n=== artista equivocado con titulo correcto ===")
client, _ = build({"recording-list": [recording("Roller", "Grupo Inventado", 99)]})
check("artista no cuadra -> None", client.lookup("Apache 207", "Roller") is None)

print("\n=== prefiere album original sobre recopilatorio ===")
client, _ = build({"recording-list": [{
    "id": "x", "title": "Roller", "ext:score": "100",
    "artist-credit": [{"artist": {"name": "Apache 207"}}],
    "release-list": [
        {"title": "Grandes Exitos", "date": "2022",
         "release-group": {"primary-type": "Album", "secondary-type-list": ["Compilation"]}},
        {"title": "Treppenhaus", "date": "2020",
         "release-group": {"primary-type": "Album", "secondary-type-list": []}},
    ],
}]})
match = client.lookup("Apache 207", "Roller")
check("evita el recopilatorio", match and match["album"] == "Treppenhaus", match)

print("\n=== la cache evita repetir peticiones ===")
client, fake = build({"recording-list": [recording("Roller", "Apache 207", 100)]})
client.lookup("Apache 207", "Roller")
client.lookup("apache 207", "ROLLER")
check("2 busquedas -> 1 peticion", fake.calls == 1, "peticiones=%d" % fake.calls)
check("cuenta el acierto de cache", client.cache_hits == 1)

print("\n=== la cache sobrevive al reinicio ===")
client.close()
cache_file = client.cache.path
client2 = mb.MusicBrainzClient(cache_path=cache_file)
fake2 = FakeMB({"recording-list": []})
mb.musicbrainzngs.search_recordings = fake2.search_recordings
match = client2.lookup("Apache 207", "Roller")
check("lee del disco sin pedir nada", match is not None and fake2.calls == 0, match)

print("\n=== reintento con espera ante fallo de red ===")
class Flaky:
    def __init__(self):
        self.calls = 0

    def search_recordings(self, **kwargs):
        self.calls += 1
        if self.calls < 3:
            raise mb.musicbrainzngs.NetworkError("503")
        return {"recording-list": [recording("Roller", "Apache 207", 100)]}

flaky = Flaky()
mb.musicbrainzngs.search_recordings = flaky.search_recordings
mb.time.sleep = lambda seconds: None
tmp = Path(tempfile.mkdtemp()) / "c.json"
client3 = mb.MusicBrainzClient(cache_path=tmp)
match = client3.lookup("Apache 207", "Roller")
check("reintenta y acaba acertando", match is not None and flaky.calls == 3,
      "intentos=%d" % flaky.calls)

print("\n=== la guarda del contacto ===")
real = mb.CONTACT
check("un contacto real pasa la guarda", mb.contact_is_configured())
mb.CONTACT = "PON-AQUI-TU-CORREO@example.com"
check("el placeholder se detecta", not mb.contact_is_configured())
mb.CONTACT = "sin-arroba"
check("una cadena sin arroba se detecta", not mb.contact_is_configured())
mb.CONTACT = real

print("\n%s\n%d correctas, %d fallos\n%s" % ("=" * 46, passed, failed, "=" * 46))
sys.exit(1 if failed else 0)

"""Pruebas de nombres, duplicados, generos y portadas. No tocan la red."""

import json
import sys
import tempfile
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tagger import artists as artists_mod
from tagger import covers as covers_mod
from tagger.duplicates import _same_length, _split_by_duration
from tagger.genre_check import canonical_genres, check
from tagger.renaming import clean_name, plan_renames
from tagger.filenames import folder_hints
from tagger.text import (clean_pair, credit_matches, norm, overlap, same_text,
                         preserve_artist_case, strip_noise,
                         title_search_variants)
from tagger import deezer, itunes, lastfm
from tagger.providers import Providers, album_key
from tagger import pipeline

passed = failed = 0


def check_that(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  OK   %s" % name)
    else:
        failed += 1
        print("  FALLO %s  %s" % (name, detail))


def equals(name, got, want):
    check_that(name, got == want, "obtuve %r, esperaba %r" % (got, want))


print("=== nombres: se quita el ruido de video y live ===")
equals("[Official Video]", clean_name("Montez - verletzte menschen [Official Video]"),
       "Montez - verletzte menschen")
equals("(Live)", clean_name("Queen - Bohemian Rhapsody (Live)"),
       "Queen - Bohemian Rhapsody")
equals("(En Vivo)", clean_name("Soda Stereo - De Musica Ligera (En Vivo)"),
       "Soda Stereo - De Musica Ligera")
equals("acento en el ruido", clean_name("Vingadora - Metralhadora (Áudio Oficial)"),
       "Vingadora - Metralhadora")

print("\n=== nombres: se conserva lo que identifica la grabacion ===")
equals("(Radio Edit)", clean_name("Eric Prydz - Call On Me (Radio Edit)"),
       "Eric Prydz - Call On Me (Radio Edit)")
equals("(Remix)", clean_name("Aaron Smith - Dancin (KRONO Remix)"),
       "Aaron Smith - Dancin (KRONO Remix)")
equals("'live' dentro del titulo", clean_name("Wings - Live and Let Die"),
       "Wings - Live and Let Die")
equals("'livin' dentro del titulo", clean_name("Ricky Martin - Livin la Vida Loca"),
       "Ricky Martin - Livin la Vida Loca")

print("\n=== nombres: caracteres raros ===")
equals("emoji", clean_name("Kinderlied Kuken \U0001F423 - TiRiLi"), "Kinderlied Kuken - TiRiLi")
equals("kbps pegado", clean_name("DAMAS GRATIS - MARITA_320kbps"), "DAMAS GRATIS - MARITA")
equals("comillas tipograficas", clean_name("Avicii - Without You \u201cAudio\u201d"),
       "Avicii - Without You")
equals("ruido pegado sin separador", clean_name("Lune - KINDER DEINER SONNE Lyrics"),
       "Lune - KINDER DEINER SONNE")
equals("no vacia un titulo que ES la palabra", clean_name("Video"), "Video")
equals("conserva el titulo completo", clean_name("Frenzy - Lampen an"),
       "Frenzy - Lampen an")
equals("guion largo", clean_name("ZYMBA \u2013 OVER"), "ZYMBA - OVER")
equals("punto final", clean_name("KAYEF - OMA M."), "KAYEF - OMA M")
equals("web spam", clean_name("Tema [www.musica.com]"), "Tema")
check_that("nunca deja el nombre vacio", clean_name("(Official Video)") == "")

print("\n=== nombres: puntuacion que dejan los descargadores ===")
equals("(OUT NOW_) con guion bajo",
       clean_name("Hardwell feat. Chris Jones - Young Again (OUT NOW_) #UnitedWeAre"),
       "Hardwell feat. Chris Jones - Young Again")
equals("(Audio.) con punto", clean_name("Tema (Official Audio.)"), "Tema")
equals("[Official Video!]", clean_name("Tema [Official Video!]"), "Tema")
equals("hashtag promocional del final", clean_name("Tema #hit #viral"), "Tema")
equals("hashtag que ES el titulo", clean_name("#SELFIE- The Chainsmokers"),
       "#SELFIE- The Chainsmokers")
equals("arrastra el NO colgante",
       clean_name("LA PIEDRA URBANA - PRINCESA 2012 NO OFFICIAL"),
       "LA PIEDRA URBANA - PRINCESA 2012")
equals("parentesis de ruido truncado",
       strip_noise("Eh zu kurz (Offizielles Musikvideo"), "Eh zu kurz")
equals("descripcion y traduccion entre corchetes",
       strip_noise("Katjuscha [German version of Russian song][+English translation]"),
       "Katjuscha")

corrections_before = pipeline.CORRECTIONS_PATH
pipeline.CORRECTIONS_PATH = Path("correcciones.json")
equals("correccion manual para un opening ambiguo",
       pipeline.identity_correction(Path("Digimon - Staffel 1 - Opening.mp3")),
       ("Frank Schindel", "Leb deinen Traum"))
pipeline.CORRECTIONS_PATH = corrections_before

print("\n=== nombres: no se pisan entre si ===")
with tempfile.TemporaryDirectory() as tmp:
    folder = Path(tmp)
    for name in ["Tema (Official Video).mp3", "Tema (Official Audio).mp3", "Tema.mp3"]:
        (folder / name).write_bytes(b"x")
    planned = plan_renames(sorted(folder.glob("*.mp3")))
    targets = [t.name for _, t in planned]
    check_that("sin colisiones", len(set(targets)) == len(targets), targets)
    check_that("no pisa un archivo existente",
               "Tema.mp3" not in targets, targets)

print("\n=== duplicados: la duracion separa versiones ===")
check_that("126s y 124s son la misma", _same_length(126.4, 124.7))
check_that("180s y 200s no lo son", not _same_length(180.0, 200.0))
groups = _split_by_duration([
    {"duration": 180.0}, {"duration": 181.0}, {"duration": 240.0},
])
equals("parte en dos grupos", sorted(len(g) for g in groups), [1, 2])

print("\n=== duplicados: el caso real que fallaba ===")
uno = clean_pair("Magic Phonk Records", "Phonk - OVERDOSE (Magic Phonk Release)")
dos = clean_pair("Magic Phonk Records", "Phonk - Pentagramma (Magic Phonk Release)")
check_that("OVERDOSE y Pentagramma no se confunden", uno[1] != dos[1],
           "%r vs %r" % (uno[1], dos[1]))

print("\n=== generos: los paraguas no son prueba ===")
root = Path("D:/Musica")
check_that("reggaeton con tag Pop no avisa",
           check(root / "regueton" / "x.mp3", root, "Pop") is None)
check_that("cumbia con tag Latin no avisa",
           check(root / "cumbia" / "x.mp3", root, "Latin") is None)
result = check(root / "pop" / "x.mp3", root, "Hip-Hop/Rap")
check_that("pop con tag Hip-Hop si avisa", result is not None
           and result["suggested"] == "hiphop-rap", result)
check_that("carpeta de idioma se ignora",
           check(root / "Aleman" / "x.mp3", root, "Cumbia") is None)
equals("vocabulario del sitio de descarga",
       sorted(canonical_genres("Dance/Electronic, Pop")), ["Electronic", "Pop"])

print("\n=== portadas ===")


class FakeResponse:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content
        self.ok = 200 <= status < 300


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self.routes.get(url, FakeResponse(404))

    def close(self):
        pass


covers_mod.MIN_INTERVAL = 0
IMAGE = b"\xff\xd8\xff" + b"j" * 5000
RELEASE = "11111111-1111-1111-1111-111111111111"
GROUP = "22222222-2222-2222-2222-222222222222"


def build_client(routes):
    tmp = Path(tempfile.mkdtemp()) / "cover.json"
    client = covers_mod.CoverArtClient(cache_path=tmp)
    client.session = FakeSession(routes)
    return client


url_release = "%s/release/%s/front-500" % (covers_mod.BASE, RELEASE)
url_group = "%s/release-group/%s/front-500" % (covers_mod.BASE, GROUP)

client = build_client({url_release: FakeResponse(200, IMAGE)})
equals("descarga por edicion", client.fetch(RELEASE, GROUP), IMAGE)

client = build_client({url_group: FakeResponse(200, IMAGE)})
data = client.fetch(RELEASE, GROUP)
check_that("cae al grupo de edicion si la edicion no tiene", data == IMAGE)

client = build_client({})
check_that("404 en ambos devuelve None", client.fetch(RELEASE, GROUP) is None)

client = build_client({url_release: FakeResponse(200, b"tiny")})
check_that("una imagen de 4 bytes no vale", client.fetch(RELEASE, "") is None)

client = build_client({url_release: FakeResponse(200, IMAGE)})
client.fetch(RELEASE, "")
antes = len(client.session.calls)
client.fetch(RELEASE, "")
check_that("la cache evita repetir la resolucion",
           client.cache_hits == 1, "hits=%d" % client.cache_hits)

check_that("portada de 0 bytes cuenta como ausente",
           covers_mod.needs_cover({"cover_bytes": 0}))
check_that("portada de 200 KB cuenta como valida",
           not covers_mod.needs_cover({"cover_bytes": 200 * 1024}))
equals("detecta PNG", covers_mod.guess_mime(b"\x89PNG\r\n\x1a\n"), "image/png")
equals("detecta JPEG", covers_mod.guess_mime(b"\xff\xd8\xff"), "image/jpeg")

canvas = Image.new("RGB", (500, 500), "white")
draw = ImageDraw.Draw(canvas)
draw.rectangle((0, 100, 499, 399), fill="navy")
draw.ellipse((180, 160, 320, 300), fill="orange")
buffer = io.BytesIO()
canvas.save(buffer, format="PNG")
check_that("detecta miniatura 16:9 dentro de PNG cuadrado",
           "bandas" in covers_mod.bad_cover(buffer.getvalue()))

canvas = Image.new("RGB", (500, 500), "navy")
draw = ImageDraw.Draw(canvas)
for y in range(500):
    draw.line((0, y, 499, y), fill=(y % 255, 80, 160))
buffer = io.BytesIO()
canvas.save(buffer, format="PNG")
equals("una caratula cuadrada sin bandas sigue siendo valida",
       covers_mod.bad_cover(buffer.getvalue()), "")

print("\n=== cache negativa ===")
for module, cls in ((deezer, deezer.DeezerClient),
                    (itunes, itunes.ItunesClient),
                    (lastfm, lastfm.LastfmClient)):
    client = cls(Path(tempfile.mkdtemp()) / "cache.json")
    client._put("Nadie", "Nada", None)
    stored = client.data[client.key("Nadie", "Nada")]
    check_that("%s versiona el fallo" % module.__name__.split(".")[-1],
               stored == {module.MISS: module.SEARCH_VERSION}, stored)
    check_that("%s no reintenta el fallo nuevo" % module.__name__.split(".")[-1],
               not client._es_fallo_viejo(stored), stored)
    client.close()

client = deezer.DeezerClient(Path(tempfile.mkdtemp()) / "deezer.json")
client.data[client.key("Artista", "Tema")] = {
    "title": "Tema", "artist": "Artista", "album": "Disco",
    "album_id": 77, "duration": 180.0, "date": "", "genres": [],
    "_details_checked": False, "source": "deezer",
}
client._get = lambda url: {
    "release_date": "2020-02-03", "record_type": "album",
    "genres": {"data": [{"name": "Pop"}]},
}
_ajeno = {"data": [{
    "title": "Rolling 200 Deep", "artist": {"name": "DJ Kay Slay"},
    "album": {"title": "Rolling 200 Deep", "id": 1}, "duration": 3753, "rank": 9,
}]}
_propio = {"data": [{
    "title": "Critical", "artist": {"name": "CHEZ"},
    "album": {"title": "Critical", "id": 2, "cover_big": "x"},
    "duration": 167, "rank": 1,
}]}
_libre = deezer.DeezerClient(Path(tempfile.mkdtemp()) / "deezer.json")
_libre._get = lambda url: _ajeno if "artist%3A" in url else _propio
_hallado = _libre.lookup("CHEZ", "Critical", duration=166, want_date=False)
equals("si la busqueda por campos solo trae temas ajenos, se prueba en texto libre",
       (_hallado or {}).get("album"), "Critical")

completo = client.lookup("Artista", "Tema", duration=180, want_date=True)
equals("una cache de portada luego completa la fecha", completo["date"],
       "2020-02-03")
equals("una cache de portada luego completa el genero", completo["genres"],
       ["Pop"])

client.data[client.key("Artista", "Otra version")] = {
    "title": "Otra version", "artist": "Artista", "album": "Disco corto",
    "duration": 100.0, "source": "deezer", "_details_checked": False,
}
client._get = lambda url: {"data": [{
    "title": "Otra version", "artist": {"name": "Artista"},
    "album": {"title": "Disco largo", "id": 88, "cover_big": "x"},
    "duration": 200, "rank": 1,
}]}
otra = client.lookup("Artista", "Otra version", duration=200,
                     want_date=False)
equals("una duracion distinta vuelve a buscar otra version",
       otra["album"], "Disco largo")
client.close()

print("\n=== candidatas de portada ===")
providers = Providers(Path(tempfile.mkdtemp()))


class FakeCoverArt:
    def __init__(self):
        self.called = None

    def fetch(self, release_id, release_group_id=""):
        self.called = (release_id, release_group_id)
        return IMAGE

    def close(self):
        pass


providers.coverart = FakeCoverArt()
candidate_match = {
    "release_id": "id-del-ganador",
    "artwork_candidates": [{
        "source": "musicbrainz", "url": "",
        "release_id": "id-de-la-candidata", "release_group_id": "grupo-correcto",
    }],
}
providers.fetch_artwork(candidate_match)
equals("MusicBrainz usa los IDs de la candidata, no los del ganador",
       providers.coverart.called, ("id-de-la-candidata", "grupo-correcto"))
providers.close()

print("\n=== consenso sin artista ===")


class FakeLookup:
    requests = cache_hits = errors = 0

    def lookup(self, *args, **kwargs):
        return {
            "title": "Staffel 1 Opening", "artist": "SHINee",
            "album": "SHINee WORLD", "artwork_url": "x", "source": "deezer",
        }

    def close(self):
        pass


providers = Providers.__new__(Providers)
providers.init_runtime_state()
providers.deezer = FakeLookup()
providers.itunes = providers.musicbrainz = providers.lastfm = None
providers.hits = {"deezer": 0, "itunes": 0, "musicbrainz": 0, "lastfm": 0}
providers._variantes = lambda artist, title, hints=(), max_variants=None: [("", title)]
check_that("Deezer no cuela un artista distinto en el intento libre",
           providers.consensus_lookup("Digimon", "Staffel 1 Opening") is None)

print("\n=== ruido pegado al final del nombre ===")
equals("con letra entero", clean_name("QUIERO QUEMAR EL GALLINERO con letra"),
       "QUIERO QUEMAR EL GALLINERO")
equals("Official Music Video entero",
       clean_name("Red Hot Chili Peppers Cant Stop Official Music Video"),
       "Red Hot Chili Peppers Cant Stop")
equals("detras de una comilla", clean_name("NewJeans - 'GODS' Official Audio"),
       "NewJeans - 'GODS'")
equals("pegado a un parentesis",
       clean_name("Post Malone - Better Now (beerbongs & bentleys)Official Audio"),
       "Post Malone - Better Now (beerbongs & bentleys)")
equals("en espanol entero", clean_name("abba-chiquitita-en-espanol"), "abba-chiquitita")
equals("subtitulado con guiones",
       clean_name("jeena-yahan-mi-nombre-es-joker-subtitulado-al-espanol"),
       "jeena-yahan-mi-nombre-es-joker")

print("\n=== y lo que NO es ruido ===")
equals("5 Letras es el titulo", clean_name("Alexis & Fido - 5 Letras"),
       "Alexis & Fido - 5 Letras")
equals("Cinco Letras tambien", clean_name("Alexis Y Fido-Cinco Letras"),
       "Alexis Y Fido-Cinco Letras")
equals("pero 150 Letra en singular si es ruido",
       clean_name("Yandel Feid Yandel 150 Letra"), "Yandel Feid Yandel 150")
equals("Unofficial no es Official", clean_name("Unofficial Business"), "Unofficial Business")
equals("Radio no es Audio", clean_name("Radio Ga Ga"), "Radio Ga Ga")
equals("un Soundtrack de verdad se conserva",
       clean_name("Irene Cara - Flashdance (Original Soundtrack 1983)"),
       "Irene Cara - Flashdance (Original Soundtrack 1983)")

print("\n=== el artista pegado al titulo sin separador claro ===")
equals("guion sin espacios", clean_pair("Bob Marley", "Bob Marley-Bad Boys (OfficialMusic)"),
       ("Bob Marley", "Bad Boys"))
equals("guion con espacio solo detras", clean_pair("Charles Ans", "Charles Ans- Un Viaje"),
       ("Charles Ans", "Un Viaje"))
equals("artista delante sin separador",
       clean_pair("la champions liga", "la champions liga besame"),
       ("la champions liga", "besame"))
equals("artista detras sin separador",
       clean_pair("Los Pericos", "Runaway Los Pericos"),
       ("Los Pericos", "Runaway"))
equals("no se corta si no queda titulo",
       clean_pair("Katyusha", "Katyusha"), ("Katyusha", "Katyusha"))
equals("guion de verdad dentro del titulo",
       clean_pair("Michael Jackson", "Spider-Man"), ("Michael Jackson", "Spider-Man"))

print("\n=== ruido en frances, ruso y pegado al titulo ===")
equals("(Audio Officiel)", strip_noise("Fais comme ca (Audio Officiel)"), "Fais comme ca")
equals("[HD + texto en ruso]", strip_noise("Вальс [HD + Текст]"), "Вальс")
equals("(Premiera del tema)", strip_noise("Счастливыми (Премьера трека 2023)"), "Счастливыми")
equals("sub. espanol con punto", strip_noise("katyusha [ sub. español ]"), "katyusha")
equals("(OfficialMusic) pegado", strip_noise("Bad Boys (OfficialMusic)"), "Bad Boys")
equals("(Remix) sigue vivo", strip_noise("Yad (Tiktok Remix)"), "Yad (Tiktok Remix)")
equals("(video no oficial, ayudad con los derechos)",
       strip_noise("Бременские Музыканты (неофициальное видео, помогите получить права)"),
       "Бременские Музыканты")
equals("(Неофициальный клип)", strip_noise("Песня (Неофициальный клип)"), "Песня")

print("\n=== el folk de los catalogos no es folklore ===")
from tagger.genre_check import genre_from_online
equals("indie con una etiqueta folk no acaba de folklore",
       genre_from_online(["Alternativo", "Indie Pop", "Pop", "Indie Pop/Folk"]),
       "Rock")
equals("el folklore de verdad se conserva",
       genre_from_online(["Pop", "Folklore"]), "Folklore")
equals("folk a secas sigue dando genero", genre_from_online(["Folk"]), "Folklore")

print("\n=== cirilico y latino son el mismo nombre ===")
check_that("Катюша es katyusha", same_text("Катюша", "katyusha"))
check_that("una letra de mas no cambia al artista",
           credit_matches("Damian Marley", "Damian Marleys"))
check_that("dos artistas distintos siguen siendo distintos",
           not same_text("Bob Marley", "Bob Dylan"))
check_that("cirilico contra su transcripcion en el archivo",
           overlap("Элджей & Кравц", "eldzei kravc") > 0.9)

print("\n=== campos cambiados: el titulo estaba en el artista ===")
RAIZ = Path("D:/Musica")
ruta = Path("D:/Musica/cumbia/la champions liga/ME VOY MUY LEJOS - HERNAN Y LA CHAMPIONS LIGA.mp3")
tags = {"title": "HERNAN Y LA CHAMPIONS LIGA", "artist": "ME VOY MUY LEJOS",
        "album": "ME VOY MUY LEJOS", "albumartist": "HERNAN Y LA CHAMPIONS LIGA",
        "genre": "Cumbia", "date": "2023", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
equals("el albumartist delata el cambio", (artista, titulo),
       ("HERNAN Y LA CHAMPIONS LIGA", "ME VOY MUY LEJOS"))

ruta = Path("D:/Musica/cumbia/la champions liga/solo quiero amarte la champions liga.mp3")
tags = {"title": "solo quiero amarte la champions liga", "artist": "la9enelpecho",
        "album": "solo quiero amarte la champions liga",
        "albumartist": "la9enelpecho", "genre": "Cumbia", "date": "", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
equals("el nombre del grupo dentro del titulo no es un cambio de campos",
       (artista, titulo), ("la champions liga", "solo quiero amarte"))

ruta = Path("D:/Musica/Ruso/Tëmnaja noč.mp3")
tags = {"title": "Tëmnaja noč", "artist": "Марк Бернес", "album": "Tëmnaja noč",
        "albumartist": "Марк Бернес", "genre": "", "date": "2024", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
equals("un artista correcto al que nada respalda se queda",
       (artista, titulo), ("Марк Бернес", "Tëmnaja noč"))

ruta = Path("D:/Musica/ZLast/ATARASHII GAKKO! - Oi AG!.mp3")
tags = {"title": "[가사번역] 새로운 학교의 리더즈 - Oi AG!", "artist": "감자농사",
        "album": "[가사번역] 새로운 학교의 리더즈 - Oi AG!", "albumartist": "감자농사",
        "genre": "", "date": "2026", "tracknumber": ""}
artista, titulo, fuente, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
equals("el canal que tradujo la letra no es el artista: manda el archivo",
       (artista, titulo, fuente),
       ("ATARASHII GAKKO!", "Oi AG!", "artista sin respaldo"))
equals("y el albumartist copiado del canal tampoco se conserva",
       pipeline._album_artist_fiable(tags, artista), "")

ruta = Path("D:/Musica/ZLast/ATARASHII GAKKO! - Oi AG!.mp3")
tags = {"title": "Oi AG!", "artist": "新しい学校のリーダーズ", "album": "",
        "albumartist": "新しい学校のリーダーズ", "genre": "", "date": "",
        "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
equals("un artista en otro alfabeto con el titulo limpio no se toca",
       (artista, titulo), ("新しい学校のリーダーズ", "Oi AG!"))
equals("y su albumartist se conserva",
       pipeline._album_artist_fiable(tags, artista), "新しい学校のリーダーズ")

ruta = Path("D:/Musica/Ruso/Джаро Ханза Королева танцпола.mp3")
tags = {"title": "", "artist": "", "album": "Королева танцпола",
        "albumartist": "", "genre": "Pop", "date": "2019-05-31", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
equals("sin artista, el album dice donde acaba el nombre del grupo",
       (artista, titulo), ("Джаро Ханза", "Королева танцпола"))

print("\n=== portada buscada por el nombre del album ===")


class FakeAlbumArt:
    requests = cache_hits = errors = 0

    def __init__(self):
        self.pedido = None

    def album_artwork(self, artist, album):
        self.pedido = (artist, album)
        return "http://portada"

    def fetch_artwork(self, url):
        return b"x" * 3000

    def close(self):
        pass


providers = Providers.__new__(Providers)
providers.init_runtime_state()
providers.deezer = FakeAlbumArt()
providers.itunes = providers.musicbrainz = providers.lastfm = None
providers.coverart = None
sin_imagen = {"source": "lastfm", "artist": "Hermanos Yaipen",
              "album": "Una Rosa lo Sabe", "artwork_url": ""}
datos = providers.fetch_artwork(sin_imagen)
check_that("si la fuente no trae imagen, se busca por el disco",
           datos is not None and providers.deezer.pedido ==
           ("Hermanos Yaipen", "Una Rosa lo Sabe"))

print("\n=== una imagen de formato desconocido no rompe nada ===")
webp = b"RIFF____WEBPVP8 " + b"x" * 2000
equals("lado menor desconocido vale 0", covers_mod.min_side(webp), 0)
equals("y en el informe sale como interrogante", covers_mod.size_text(webp), "?")
equals("sin datos tampoco revienta", covers_mod.min_side(b""), 0)

print("\n=== de que lado esta el artista ===")
CONOCIDOS = {"kumbia kings", "los palmeras", "el polaco", "grupo bronco"}

ruta = Path("D:/Musica/cumbia/Me Enamoré-Kumbia Kings.mp3")
tags = {"title": "Me Enamoré-Kumbia Kings", "artist": "A.B. Quintanilla III",
        "album": "", "albumartist": "A.B. Quintanilla III", "genre": "Cumbia",
        "date": "", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(
    ruta, tags, folder_hints(ruta, RAIZ), known=CONOCIDOS)
equals("gana el lado que la biblioteca ya conoce",
       (artista, titulo), ("Kumbia Kings", "Me Enamoré"))

ruta = Path("D:/Musica/cumbia/la-cola-los-palmeras.mp3")
tags = {"title": "La cola - Los Palmeras", "artist": "Sabias Que", "album": "",
        "albumartist": "Sabias Que", "genre": "Cumbia", "date": "", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(
    ruta, tags, folder_hints(ruta, RAIZ), known=CONOCIDOS)
equals("Titulo - Artista tambien se reparte bien",
       (artista, titulo), ("Los Palmeras", "La cola"))

ruta = Path("D:/Musica/Aleman/FEUERSCHWANZ - Dragostea Din Tei_ Napalm Records.mp3")
tags = {"title": "Dragostea Din Tei", "artist": "FEUERSCHWANZ",
        "album": "FEUERSCHWANZ",
        "albumartist": "Dragostea Din Tei (Video) | Napalm Records",
        "genre": "Metal", "date": "2022", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
equals("un albumartist que es el sello no cambia nada",
       (artista, titulo), ("FEUERSCHWANZ", "Dragostea Din Tei"))

ruta = Path("D:/Musica/cumbia/kumbia kings-all starz/Kumbia All-Starz (feat. Luis Enrique) - Invisible.mp3")
tags = {"title": "Kumbia All-Starz - Invisible",
        "artist": "A.B. Quintanilla III feat. Luis Enrique",
        "album": "La Vida de un Genio", "albumartist": "A.B. Quintanilla III",
        "genre": "Cumbia", "date": "2010", "tracknumber": "0"}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
check_that("un bloque \"Grupo - Cancion\" no puede acabar de artista",
           " - " not in artista)

ruta = Path("D:/Musica/electro/Heavenly (SYNTHWAVE, CHILL, EDM, CLUB).mp3")
tags = {"title": "Heavenly (SYNTHWAVE, CHILL, EDM, CLUB) HIT 2025 | No copyright / Royalty-Free",
        "artist": "L-one Music", "album": "", "albumartist": "L-one Music",
        "genre": "", "date": "", "tracknumber": ""}
artista, titulo, _f, _c = pipeline.resolve_identity(ruta, tags, folder_hints(ruta, RAIZ))
check_that("un titulo enorme del descargador no se parte en artista",
           len(artista) <= 60 and "|" not in artista)

print("\n=== registro de portadas ya puestas ===")
carpeta_reg = Path(tempfile.mkdtemp())
archivo_reg = carpeta_reg / "cancion.mp3"
archivo_reg.write_bytes(b"audio")
registro = covers_mod.CoverLedger(carpeta_reg / "portadas.json")
imagen = b"IMAGEN" * 500
check_that("al principio no hay nada apuntado",
           not registro.already_done(archivo_reg, imagen))
registro.record(archivo_reg, imagen, "deezer+itunes")
check_that("apuntada, se da por hecha",
           registro.already_done(archivo_reg, imagen))
registro.data[str(archivo_reg)]["version"] = 1
registro.data[str(archivo_reg)]["fuente"] = "deezer"
check_that("una portada antigua de una fuente se vuelve a revisar",
           not registro.already_done(archivo_reg, imagen))
registro.data[str(archivo_reg)]["fuente"] = "deezer+itunes"
check_that("un consenso antiguo TAMBIEN se revisa: el fallo estaba en de donde "
           "salia la imagen, no en cuantas fuentes acertaron el album",
           not registro.already_done(archivo_reg, imagen))
registro.data[str(archivo_reg)]["fuente"] = covers_mod.MANUAL_SOURCE
check_that("una portada elegida a mano no caduca al subir la version",
           registro.already_done(archivo_reg, imagen))
registro.data[str(archivo_reg)]["fuente"] = "deezer+itunes"
registro.data[str(archivo_reg)]["version"] = covers_mod.LEDGER_VERSION
check_that("otra imagen distinta no cuenta",
           not registro.already_done(archivo_reg, b"OTRA" * 500))
archivo_reg.write_bytes(b"audio cambiado")
check_that("si alguien toca el archivo, se vuelve a comprobar",
           not registro.already_done(archivo_reg, imagen))
registro.flush()
otro = covers_mod.CoverLedger(carpeta_reg / "portadas.json")
check_that("el registro sobrevive entre ejecuciones", bool(otro.data))

print("\n=== el veredicto de la portada se memoriza ===")
covers_mod._VERDICTOS.clear()
_bad_real = covers_mod._bad_cover_uncached
_veces = []
covers_mod._bad_cover_uncached = lambda d: (_veces.append(1), "")[1]
covers_mod.bad_cover(b"z" * 4000)
covers_mod.bad_cover(b"z" * 4000)
equals("la misma imagen no se analiza dos veces", len(_veces), 1)
covers_mod.bad_cover(b"y" * 4000)
equals("otra imagen si se analiza", len(_veces), 2)
covers_mod._bad_cover_uncached = _bad_real
covers_mod._VERDICTOS.clear()

print("\n=== la cache de lecturas no sirve datos viejos ===")
from tagger import tags as tags_mod

carpeta_tmp = Path(tempfile.mkdtemp())
archivo_tmp = carpeta_tmp / "prueba.mp3"
archivo_tmp.write_bytes(b"a" * 100)
lecturas = []
_original = tags_mod._read_tags_uncached


def _contando(path):
    lecturas.append(str(path))
    return {"title": "uno"}


tags_mod._read_tags_uncached = _contando
tags_mod.clear_caches()
tags_mod.read_tags(archivo_tmp)
tags_mod.read_tags(archivo_tmp)
equals("la segunda lectura sale de la cache", len(lecturas), 1)
archivo_tmp.write_bytes(b"b" * 250)
tags_mod.read_tags(archivo_tmp)
equals("si el archivo cambia, se vuelve a leer", len(lecturas), 2)
tags_mod.forget(archivo_tmp)
tags_mod.read_tags(archivo_tmp)
equals("forget obliga a releer", len(lecturas), 3)
devuelto = tags_mod.read_tags(archivo_tmp)
devuelto["title"] = "tocado"
equals("quien recibe los tags no puede ensuciar la cache",
       tags_mod.read_tags(archivo_tmp)["title"], "uno")
tags_mod._read_tags_uncached = _original
tags_mod.clear_caches()

print("\n=== la duracion reutiliza la lectura de tags ===")
archivo_audio = carpeta_tmp / "audio-falso.mp3"
archivo_audio.write_bytes(b"audio")
_mutagen_original = tags_mod.MutagenFile
aperturas = []


class _InfoFalsa:
    length = 123.5
    bitrate = 192000


class _AudioFalso:
    info = _InfoFalsa()
    tags = {}


tags_mod.MutagenFile = lambda *args, **kwargs: aperturas.append(1) or _AudioFalso()
tags_mod.clear_caches()
tags_mod.read_tags(archivo_audio)
info_basica = tags_mod.read_basic_audio_info(archivo_audio)
equals("tags y duracion abren el archivo una sola vez", len(aperturas), 1)
equals("la duracion guardada es correcta", info_basica["duration"], 123.5)
tags_mod.forget(archivo_audio)
tags_mod.read_basic_audio_info(archivo_audio)
equals("al escribir se invalida tambien la duracion", len(aperturas), 2)
tags_mod.MutagenFile = _mutagen_original
tags_mod.clear_caches()

print("\n=== busqueda robusta por variantes y pistas ===")
equals("el credito pegado acepta al artista principal",
       credit_matches("David Guetta", "David Guetta Bebe Rexha"), True)
check_that("Veridis Quo tolera los 12 s del video",
           deezer.duration_tolerance(357) >= 12)
check_that("recorta la cola del Mundial",
           "Hayya Hayya (Better Together)" in title_search_variants(
               "Hayya Hayya (Better Together) FIFA World Cup 2022TM Official Soundtrack"))
check_that("quita HD sin perder Club Mix",
           "Stay With Me (Club Mix)" in title_search_variants(
               "Stay With Me HD (Club Mix)"))
check_that("corrige A-Track solo para buscar",
           "Heads Will Roll (A-Trak Remix)" in title_search_variants(
               "Heads Will Roll (A-Track Remix)"))

variants = Providers._variantes(
    None, "Project X", "Yeah Yeah Yeahs - Heads Will Roll (A-Track Remix)",
    hints=("Yeah Yeah Yeahs",))
check_that("usa albumartist como pista",
           any(a == "Yeah Yeah Yeahs" for a, _ in variants), variants)
check_that("parte el artista incrustado en el titulo",
           any(a == "Yeah Yeah Yeahs" and t.startswith("Heads Will Roll")
               for a, t in variants), variants)
variants = Providers._variantes(
    None, "Umi no Mieru Machi A Town With An Ocean View",
    "Kikis Delivery Service Main Theme", hints=("Joe Hisaishi",))
check_that("prueba el supuesto artista largo como titulo",
           ("", "Umi no Mieru Machi") in variants, variants)
check_that("prueba tambien la traduccion pegada al final",
           ("", "A Town With An Ocean View") in variants, variants)
check_that("combina la pista de carpeta con la traduccion",
           ("Joe Hisaishi", "A Town With An Ocean View") in variants, variants)

check_that("sin artista rechaza un homonimo once segundos mas largo",
           not Providers._coincidencia_exacta(Providers.__new__(Providers), {
               "title": "Umi no Mieru Machi",
               "artist": "Ayuka Watanabe",
               "duration": 252,
           }, "", "Umi no Mieru Machi", 241))
check_that("sin artista acepta la version a tres segundos",
           Providers._coincidencia_exacta(Providers.__new__(Providers), {
               "title": "A Town With An Ocean View",
               "artist": "Joe Hisaishi",
               "duration": 238,
           }, "", "A Town With An Ocean View", 241))

probe = Providers.__new__(Providers)
probe.init_runtime_state()
probe.deezer = probe.itunes = probe.musicbrainz = probe.lastfm = None
probe._variantes = lambda artist, title, hints=(), max_variants=None: [
    ("Artista", "Titulo ruidoso"), ("Artista", "Titulo limpio")]
probe._ask_parallel = lambda names, art, title, duration, want_date: [{
    "title": title, "artist": art,
    "album": "" if "ruidoso" in title else "Album real",
    "source": "musicbrainz",
}]
probe._ask = lambda *args, **kwargs: None
found = probe._gather("Artista", "Titulo ruidoso")
equals("una respuesta sin album no bloquea la variante siguiente",
       found[0]["album"], "Album real")

probe._variantes = lambda artist, title, hints=(), max_variants=None: [
    ("Artista", "Uno"), ("Artista", "Dos"), ("Artista", "Tres")]
probe._ask_parallel = lambda *args, **kwargs: []
source_calls = []
probe._ask = lambda name, *args, **kwargs: source_calls.append(name) or None
equals("un fallo total no consulta iTunes a ciegas",
       probe._gather("Artista", "Titulo"), [])
check_that("iTunes no frena los archivos que nadie conoce",
           "itunes" not in source_calls, source_calls)

probe._variantes = lambda artist, title, hints=(), max_variants=None: [(artist, title)]
probe._ask_parallel = lambda *args, **kwargs: [
    {"title": "Tema", "artist": "Artista", "album": "Disco",
     "source": "deezer"},
    {"title": "Tema", "artist": "Artista", "album": "Disco - Single",
     "source": "lastfm"},
]
late_calls = []
probe._ask = lambda name, *args, **kwargs: late_calls.append(name) or None
equals("dos fuentes rapidas bastan para el consenso",
       len(probe._gather("Artista", "Tema")), 2)
equals("con acuerdo rapido no espera MusicBrainz ni iTunes", late_calls, [])

probe._ask_parallel = lambda *args, **kwargs: [
    {"title": "Tema", "artist": "Artista", "album": "Disco",
     "source": "deezer"},
    {"title": "Tema", "artist": "Artista", "album": "Otro",
     "source": "lastfm"},
]
late_calls = []
probe._ask = lambda name, *args, **kwargs: (
    late_calls.append(name) or
    ({"title": "Tema", "artist": "Artista", "album": "Disco",
      "source": "musicbrainz"} if name == "musicbrainz" else None))
probe._gather("Artista", "Tema")
equals("si discrepan, MusicBrainz desempata", late_calls, ["musicbrainz"])

resultado = probe._consensus([
    {"title": "Tema", "artist": "Artista", "album": "Disco",
     "source": "lastfm", "artwork_url": "portada-correcta"},
    {"title": "Tema", "artist": "Artista & Invitada", "album": "Disco - Single",
     "source": "itunes", "artwork_url": "portada-correcta-2"},
    {"title": "Tema", "artist": "Artista", "album": "Recopilatorio",
     "source": "deezer", "artwork_url": "portada-ajena"},
], artist="Artista", title="Tema", duration=180)
equals("dos ediciones del mismo disco cuentan como acuerdo",
       resultado["agree"], 2)
equals("el consenso conserva el credito mas completo",
       resultado["artist"], "Artista & Invitada")
check_that("una alternativa perdedora no presta su portada",
           all(c["url"] != "portada-ajena"
               for c in resultado["artwork_candidates"]))
equals("las ediciones equivalentes no salen como alternativas",
       resultado["alternatives"], ["Recopilatorio"])
equals("Official Soundtrack no separa el mismo album",
       album_key("FIFA World Cup Qatar 2022 (Official Soundtrack)"),
       album_key("FIFA World Cup Qatar 2022"))

print("\n=== mayusculas deliberadas de los artistas ===")
equals("BABYMETAL no se convierte en Babymetal",
       preserve_artist_case("Babymetal", ("BABYMETAL",)), "BABYMETAL")
equals("tambien funciona dentro de una colaboracion",
       preserve_artist_case("Five Finger Death Punch feat. Babymetal",
                            ("rock/BABYMETAL",)),
       "Five Finger Death Punch feat. BABYMETAL")
equals("conserva una grafia mixta como LiL CaKe",
       preserve_artist_case("Lil Cake", ("LiL CaKe - MERCHO",)), "LiL CaKe")
equals("una carpeta toda minuscula no degrada la fuente",
       preserve_artist_case("David Guetta", ("electro/david guetta",)),
       "David Guetta")
_read_tags_public = tags_mod.read_tags
_case_tags = {
    "a": {"artist": "BABYMETAL", "albumartist": "BABYMETAL"},
    "b": {"artist": "BABYMETAL", "albumartist": "BABYMETAL"},
    "c": {"artist": "Babymetal", "albumartist": ""},
}
tags_mod.read_tags = lambda path: _case_tags[path]
spellings = artists_mod.canonical_artist_spellings(list(_case_tags))
equals("la mayoria de la biblioteca decide la grafia canonica",
       spellings["babymetal"], "BABYMETAL")
tags_mod.read_tags = _read_tags_public

print("\n=== carpetas tematicas no son artistas ===")
check_that("Brasil se reconoce como categoria",
           artists_mod._obvious_category_name("brasil"))
check_that("una carpeta de decadas es categoria",
           artists_mod._obvious_category_name("70-80-90"))
check_that("una errata leve sigue respaldando al artista",
           artists_mod._folder_matches_artist("marron 5", "Maroon 5"))
check_that("un artista normal no parece categoria",
           not artists_mod._obvious_category_name("David Guetta"))

print("\n=== registro de canciones ya verificadas ===")
from tagger.verification import VerificationLedger, VERIFICATION_VERSION

_reg = Path(tempfile.mkdtemp()) / "verificadas.json"
_ledger = VerificationLedger(_reg)
_completa = {"artist": "Daft Punk", "title": "Veridis Quo", "album": "Discovery",
             "albumartist": "Daft Punk", "date": "2001", "genre": "Electronic",
             "tracknumber": "8"}
_pista = Path("D:/Musica/electro/daft punk/Daft Punk - Veridis Quo.mp3")

check_that("al principio no hay nada verificado",
           not _ledger.is_verified(_pista, _completa))
check_that("una cancion completa se apunta",
           _ledger.record(_pista, _completa, "deezer+musicbrainz"))
check_that("y en la pasada siguiente ya no se consulta",
           _ledger.is_verified(_pista, _completa))

_otra = dict(_completa, album="Random Access Memories")
check_that("si cambia un tag, se vuelve a verificar",
           not _ledger.is_verified(_pista, _otra))
check_that("si el archivo se renombra, se vuelve a verificar",
           not _ledger.is_verified(_pista.with_name("otro nombre.mp3"), _completa))

_incompleta = dict(_completa, album="")
check_that("una cancion sin album no se da por verificada",
           not _ledger.record(_pista, _incompleta))
check_that("y tampoco cuenta como verificada si le falta un campo",
           not _ledger.is_verified(_pista, _incompleta))

_ledger.flush()
_releido = VerificationLedger(_reg)
check_that("el registro sobrevive entre ejecuciones",
           _releido.is_verified(_pista, _completa))
_releido.data[str(_pista)]["version"] = VERIFICATION_VERSION + 1
check_that("subir la version del motor obliga a verificar otra vez",
           not _releido.is_verified(_pista, _completa))

print("\n=== recorrido por carpetas de primer nivel ===")
import main as main_mod

_raiz = Path(tempfile.mkdtemp())
for _nombre in ("rock", "Aleman", "_duplicados", "cumbia"):
    (_raiz / _nombre).mkdir()
(_raiz / "rock" / "x.mp3").write_bytes(b"x")
equals("las carpetas salen en orden y sin la cuarentena",
       main_mod.carpetas_de_primer_nivel(_raiz), ["Aleman", "cumbia", "rock"])
(_raiz / "suelta.mp3").write_bytes(b"x")
equals("los sueltos de la raiz se procesan al final",
       main_mod.carpetas_de_primer_nivel(_raiz),
       ["Aleman", "cumbia", "rock", ""])

print("\n=== la misma foto, aunque cambie de tamano o de formato ===")


def _portada(semilla, lado=500, formato="JPEG", calidad=92):
    lienzo = Image.new("RGB", (lado, lado), "black")
    pincel = ImageDraw.Draw(lienzo)
    for indice in range(12):
        paso = lado // 12
        pincel.rectangle(
            (indice * paso, 0, (indice + 1) * paso, lado),
            fill=((semilla * 37 + indice * 20) % 256,
                  (semilla * 11 + indice * 7) % 256,
                  (semilla * 53 + indice * 3) % 256))
    pincel.ellipse((lado // 4, lado // 4, 3 * lado // 4, 3 * lado // 4),
                   fill=(255, 255 - semilla % 255, semilla % 255))
    memoria = io.BytesIO()
    if formato == "JPEG":
        lienzo.save(memoria, format="JPEG", quality=calidad)
    else:
        lienzo.save(memoria, format=formato)
    return memoria.getvalue()


grande = _portada(1, lado=1000)
pequena = _portada(1, lado=500)
apretada = _portada(1, lado=500, calidad=35)
otra_png = _portada(1, lado=600, formato="PNG")
distinta = _portada(9, lado=500)

check_that("la misma portada a otro tamano se reconoce",
           covers_mod.same_picture(grande, pequena))
check_that("y aunque este recomprimida a lo bruto",
           covers_mod.same_picture(pequena, apretada))
check_that("y aunque cambie de JPEG a PNG",
           covers_mod.same_picture(pequena, otra_png))
check_that("dos portadas distintas NO se confunden",
           not covers_mod.same_picture(pequena, distinta))
check_that("sin imagen no hay comparacion posible",
           not covers_mod.same_picture(b"", pequena))
check_that("un dato que no es imagen no revienta ni cuela",
           not covers_mod.same_picture(b"esto no es una imagen", pequena))

print("\n=== quien puso la portada decide si se puede corregir ===")
_carpeta_prov = Path(tempfile.mkdtemp())
_mp3_prov = _carpeta_prov / "cancion.mp3"
_mp3_prov.write_bytes(b"audio")
_reg_prov = covers_mod.CoverLedger(_carpeta_prov / "portadas.json")
_img_prov = b"IMAGEN" * 500

check_that("una portada que el programa no puso no es suya",
           not _reg_prov.placed_by_program(_mp3_prov))
_reg_prov.record(_mp3_prov, _img_prov, "deezer+itunes")
check_that("la que puso el programa si lo es",
           _reg_prov.placed_by_program(_mp3_prov))
_reg_prov.record(_mp3_prov, _img_prov, covers_mod.MANUAL_SOURCE)
check_that("la elegida a mano no es del programa y no se corrige",
           not _reg_prov.placed_by_program(_mp3_prov))


_buena_1000 = _portada(3, lado=1000)
_buena_600 = _portada(3, lado=600)
_otra_1000 = _portada(8, lado=1000)
_otra_600 = _portada(8, lado=600)
_rota = b"x" * 100
_D = covers_mod

# La tabla entera contra la funcion de verdad, no contra una copia.
#   actual, candidata, la puso el programa, esta repartida -> veredicto
_TABLA = [
    ("sin portada, se pone la de la fuente",
     b"", _otra_1000, False, False, _D.COVER_PUT),
    ("una portada rota se sustituye",
     _rota, _otra_1000, False, False, _D.COVER_PUT),
    ("los mismos bytes no son cambio",
     _buena_1000, _buena_1000, False, False, _D.COVER_SAME),
    ("la misma foto mas grande mejora",
     _buena_600, _buena_1000, True, False, _D.COVER_UPGRADE),
    ("la misma foto mas pequena no toca nada",
     _buena_1000, _buena_600, True, False, _D.COVER_SAME),
    ("la misma foto del mismo tamano tampoco",
     _buena_600, _portada(3, lado=600, calidad=40), True, False, _D.COVER_SAME),
    ("otra imagen mayor, y la puso el programa: se corrige",
     _otra_600, _buena_1000, True, False, _D.COVER_FIX),
    ("otra imagen igual de grande, y la puso el programa: se corrige",
     _otra_1000, _buena_1000, True, False, _D.COVER_FIX),
    ("otra imagen menor: se conserva aunque la pusiera el programa",
     _buena_1000, _otra_600, True, False, _D.COVER_KEEP),
    ("otra imagen mayor, pero NO la puso el programa: no se toca",
     _otra_600, _buena_1000, False, False, _D.COVER_KEEP),
    ("una repartida entre discos se corrige aunque sea mayor",
     _otra_1000, _buena_600, False, True, _D.COVER_FIX),
    ("una repartida se corrige aunque no la pusiera el programa",
     _otra_600, _buena_600, False, True, _D.COVER_FIX),
    ("una repartida rota tambien se sustituye",
     _rota, _buena_600, False, True, _D.COVER_PUT),
    ("sin candidata no se decide nada",
     _buena_1000, b"", True, True, _D.COVER_NONE),
    ("sin nada de nada tampoco",
     b"", b"", False, False, _D.COVER_NONE),
]

for _titulo, _act, _cand, _prog, _rep, _esperado in _TABLA:
    equals(_titulo,
           covers_mod.decide_cover(_act, _cand, from_program=_prog,
                                   shared=_rep),
           _esperado)

check_that("los veredictos que escriben y los que no son disjuntos",
           not ({_D.COVER_PUT, _D.COVER_UPGRADE, _D.COVER_FIX}
                & {_D.COVER_KEEP, _D.COVER_SAME, _D.COVER_NONE}))

print("\n=== no bajar una portada que no puede ganar ===")
from tagger.providers import declared_side

equals("Deezer anuncia el tamano en la ruta",
       declared_side("https://cdn-images.dzcdn.net/images/cover/a/1000x1000-0.jpg"),
       1000)
equals("iTunes lo pone en el nombre",
       declared_side("https://is1-ssl.mzstatic.com/image/thumb/x.jpg/600x600bb.jpg"),
       600)
equals("Cover Art Archive lo pone tras front-",
       declared_side("https://coverartarchive.org/release/abc/front-500"), 500)
equals("Last.fm lo pone como carpeta",
       declared_side("https://lastfm-img.freetls.fastly.net/i/u/300x300/a.png"), 300)
equals("una URL que no lo dice no se adivina",
       declared_side("https://ejemplo.com/portada.jpg"), 0)
equals("sin URL no hay tamano", declared_side(""), 0)


class _FuenteConteo:
    """Cuenta descargas para comprobar que las inutiles no se piden."""

    requests = cache_hits = errors = 0

    def __init__(self):
        self.bajadas = []

    def fetch_artwork(self, url):
        self.bajadas.append(url)
        return b"x" * 4000


_prov_rend = Providers.__new__(Providers)
_prov_rend.init_runtime_state()
_prov_rend.deezer = _FuenteConteo()
_prov_rend.itunes = _prov_rend.musicbrainz = _prov_rend.lastfm = None
_prov_rend.coverart = None
_match_rend = {
    "source": "deezer", "album": "Disco", "artist": "Alguien",
    "artwork_candidates": [
        {"source": "deezer", "url": "https://cdn.dz/images/cover/a/600x600-0.jpg",
         "release_id": "", "release_group_id": ""},
    ],
}
check_that("con una portada de 1000 ya puesta, no se baja la de 600",
           _prov_rend.fetch_artwork(_match_rend, min_side_wanted=1000) is None)
equals("y no se hizo ninguna descarga", len(_prov_rend.deezer.bajadas), 0)
equals("la descarga evitada queda contada", _prov_rend.artwork_skipped, 1)
check_that("sin portada previa si se baja",
           _prov_rend.fetch_artwork(_match_rend) is not None)

print("\n=== la fecha tambien se comprueba, hacia atras ===")
_dir_fecha = Path(tempfile.mkdtemp())


class _FuenteFecha:
    """Una fuente que siempre responde lo mismo, sin tocar la red."""

    consensus_mode = True

    def __init__(self, album, fecha, confianza="alta"):
        self.respuesta = {"album": album, "date": fecha, "title": "Tema",
                          "artist": "Alguien", "confidence": confianza,
                          "agree": 2, "genres": [], "source": "prueba"}

    def consensus_lookup(self, *args, **kwargs):
        return dict(self.respuesta)


def _fecha_resultante(fecha_actual, fecha_fuente, confianza="alta"):
    ruta = _dir_fecha / "tema.mp3"
    ruta.write_bytes(b"audio")
    crudo = {"artist": "Alguien", "title": "Tema", "album": "Disco",
             "albumartist": "Alguien", "date": fecha_actual, "genre": "Pop",
             "tracknumber": "1"}
    original = tags_mod._read_tags_uncached
    tags_mod._read_tags_uncached = lambda _r: dict(crudo)
    tags_mod.clear_caches()
    try:
        propuestas, _nota = pipeline.analyze(
            ruta, _dir_fecha,
            mb_client=_FuenteFecha("Disco", fecha_fuente, confianza),
            verify=True)
    finally:
        tags_mod._read_tags_uncached = original
        tags_mod.clear_caches()
    for propuesta in propuestas:
        if propuesta.field == "date" and propuesta.writable():
            return propuesta.new
    return fecha_actual


equals("una fecha posterior es la de la descarga y se corrige",
       _fecha_resultante("2025", "2019-04-05"), "2019-04-05")
equals("una fecha anterior no se toca: puede ser la edicion original",
       _fecha_resultante("2004", "2021-02-26"), "2004")
equals("la misma fecha no genera cambio",
       _fecha_resultante("2019", "2019-04-05"), "2019")
equals("con una coincidencia dudosa no se corrige nada",
       _fecha_resultante("2025", "2019-04-05", confianza="revisar"), "2025")

print("\n=== la copia de seguridad se puede leer y deshacer ===")
_dir_bk = Path(tempfile.mkdtemp())
_bk = tags_mod.Backup(_dir_bk)
check_that("sin nada apuntado no se crea archivo", _bk.save() is None)
_bk.record(_dir_bk / "uno.mp3", {"title": "Uno", "artist": "Alguien"})
_bk.record(_dir_bk / "dos.mp3", {"title": "Dos", "artist": "Otro"})
_ruta_bk = _bk.save()
check_that("se escribe el archivo de copia", _ruta_bk is not None and _ruta_bk.exists())
_leido = json.loads(_ruta_bk.read_text(encoding="utf-8"))
equals("guarda los dos archivos", _leido.get("count"), 2)
equals("y sus tags originales",
       _leido["tags"][str(_dir_bk / "uno.mp3")]["title"], "Uno")
check_that("se vuelca solo cada FLUSH_EVERY, no en cada registro",
           tags_mod.Backup.FLUSH_EVERY > 1)

print("\n=== no intercambiar unos tags que ya estaban bien ===")
_carpeta_swap = Path(tempfile.mkdtemp())


def _identidad(nombre, artista, titulo, conocidos=None, albumartist=""):
    ruta = _carpeta_swap / nombre
    ruta.write_bytes(b"audio")
    crudo = {"artist": artista, "title": titulo, "album": "",
             "albumartist": albumartist, "date": "", "genre": "",
             "tracknumber": ""}
    a, t, _s, _c = pipeline.resolve_identity(
        ruta, crudo, {"genre_folder": "pop", "artist_folder": ""},
        known=conocidos)
    return a, t


equals("el nombre escrito al reves no basta para dar la vuelta a los tags",
       _identidad("Kinderlied Kuken _ Wo ist meine Mama _ TiRiLi - Kinderlieder.mp3",
                  "TiRiLi Kinderlieder", "Kinderlied Kuken"),
       ("TiRiLi Kinderlieder", "Kinderlied Kuken"))
equals("sin ningun testigo tampoco se arriesga a cambiarlos",
       _identidad("Oft Gefragt - AnnenMayKantereit.mp3",
                  "Oft Gefragt", "AnnenMayKantereit"),
       ("Oft Gefragt", "AnnenMayKantereit"))
equals("pero con el albumartist de testigo, si se cambian",
       _identidad("Oft Gefragt - AnnenMayKantereit.mp3",
                  "Oft Gefragt", "AnnenMayKantereit",
                  albumartist="AnnenMayKantereit"),
       ("AnnenMayKantereit", "Oft Gefragt"))
equals("y la biblioteca puede frenarlo si ya conoce al artista del tag",
       _identidad("Vaterland - Maerzfeld.mp3", "Vaterland", "Maerzfeld",
                  conocidos={"vaterland"}),
       ("Vaterland", "Maerzfeld"))

print("\n=== un canal o sello nunca es el titulo ===")
from tagger.text import strip_channel_tail

equals("el canal colgando al final se quita",
       strip_channel_tail("Wait - Warner Vault", "Warner Records Vault"), "Wait")
equals("tambien detras de una barra",
       strip_channel_tail("Dragostea Din Tei | Napalm Records", "Napalm Records"),
       "Dragostea Din Tei")
equals("no se vacia un titulo que es justo el sello",
       strip_channel_tail("Napalm Records", "Napalm Records"), "Napalm Records")
equals("un titulo normal no se toca",
       strip_channel_tail("Wait - Reprise", "Warner Records Vault"),
       "Wait - Reprise")

_carpeta_canal = Path(tempfile.mkdtemp())
_mp3_canal = _carpeta_canal / "Earshot - Wait _ Warner Vault.mp3"
_mp3_canal.write_bytes(b"audio")
_crudo = {"artist": "Earshot", "title": "Warner Records Vault",
          "album": "Earshot", "albumartist": "Warner Records Vault",
          "date": "", "genre": "", "tracknumber": ""}
_a, _t, _s, _c = pipeline.resolve_identity(
    _mp3_canal, _crudo, {"genre_folder": "rock", "artist_folder": ""})
equals("Earshot recupera su titulo real", (_a, _t), ("Earshot", "Wait"))

print("\n=== una sola grafia por album ===")
_carpeta_alb = Path(tempfile.mkdtemp())
_discos = {
    "a.mp3": "La Vida de un Genio", "b.mp3": "La Vida de un Genio",
    "c.mp3": "La Vida de un Genio", "d.mp3": "La vida de un genio",
    "e.mp3": "Solo Una Vez", "f.mp3": "empate", "g.mp3": "Empate",
}
_leidos = {}
for _nombre, _album in _discos.items():
    _ruta = _carpeta_alb / _nombre
    _ruta.write_bytes(b"audio")
    _leidos[str(_ruta)] = {"album": _album}

_original_read = tags_mod._read_tags_uncached
tags_mod._read_tags_uncached = lambda ruta: _leidos.get(str(ruta))
tags_mod.clear_caches()
_grafias = artists_mod.canonical_album_spellings(list(_carpeta_alb.glob("*.mp3")))
tags_mod._read_tags_uncached = _original_read
tags_mod.clear_caches()

equals("gana la escritura mayoritaria",
       _grafias.get(norm("la vida de un genio")), "La Vida de un Genio")
check_that("un album escrito de una sola manera no se toca",
           norm("Solo Una Vez") not in _grafias)
check_that("con empate no se decide nada",
           norm("empate") not in _grafias)

print("\n=== portadas pedidas a mas resolucion ===")
_dz = "https://cdn-images.dzcdn.net/images/cover/0a1b2c/1000x1000-000000-80-0-0.jpg"
equals("Deezer se pide a 1500", covers_mod.url_en_grande(_dz),
       "https://cdn-images.dzcdn.net/images/cover/0a1b2c/1500x1500-000000-80-0-0.jpg")
_it = "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/cd/x.jpg/600x600bb.jpg"
equals("iTunes se pide a 1500", covers_mod.url_en_grande(_it),
       "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/cd/x.jpg/1500x1500bb.jpg")
_it_3000 = _it.replace("600x600bb", "3000x3000bb")
equals("una URL que ya pide mas no se rebaja",
       covers_mod.url_en_grande(_it_3000), _it_3000)
equals("una URL de otro sitio no se toca",
       covers_mod.url_en_grande("https://cdn.dz/images/cover/a/600x600-0.jpg"),
       "https://cdn.dz/images/cover/a/600x600-0.jpg")
_dz_vacia = "https://cdn-images.dzcdn.net/images/cover//1000x1000-000000-80-0-0.jpg"
equals("Deezer sin portada no se inventa una",
       covers_mod.url_en_grande(_dz_vacia), _dz_vacia)
equals("sin URL no pasa nada", covers_mod.url_en_grande(""), "")

_prov_hd = Providers.__new__(Providers)
_prov_hd.init_runtime_state()
_prov_hd.deezer = _FuenteConteo()
_prov_hd.itunes = _prov_hd.musicbrainz = _prov_hd.lastfm = None
_prov_hd.coverart = None


def _con_url(url):
    return {"source": "deezer", "artwork_candidates": [
        {"source": "deezer", "url": url, "release_id": "",
         "release_group_id": ""}]}


_prov_hd.fetch_artwork(_con_url(_dz))
check_that("lo que se descarga es la URL grande, aunque la cache guarde la de 1000",
           _prov_hd.deezer.bajadas and "1500x1500" in _prov_hd.deezer.bajadas[0])
_prov_hd.fetch_artwork(_con_url(_dz.replace("0a1b2c", "0a1b2d")),
                       min_side_wanted=1000)
equals("frente a una de 1000 ya merece bajarla: puede venir a 1500",
       len(_prov_hd.deezer.bajadas), 2)
_prov_hd.fetch_artwork(_con_url(_dz.replace("0a1b2c", "0a1b2e")),
                       min_side_wanted=covers_mod.LADO_PEDIDO)
equals("frente a una de 1500 no", len(_prov_hd.deezer.bajadas), 2)

_cliente_caa = build_client({
    url_release.replace("front-500", "front-1200"): FakeResponse(200, IMAGE)})
_cliente_caa.size = covers_mod.LADO_CAA
_cliente_caa.cache.put(RELEASE, url_release)
equals("Cover Art Archive: la URL guardada a 500 se pide a 1200",
       _cliente_caa.fetch(RELEASE, ""), IMAGE)

_buena_1500 = _portada(3, lado=1500)
equals("la misma foto: una de 1000 ya es buena y no se reescribe por una de 1500",
       covers_mod.decide_cover(_buena_1000, _buena_1500, from_program=True),
       covers_mod.COVER_SAME)
equals("pero una de 600 si se mejora con la de 1500",
       covers_mod.decide_cover(_buena_600, _buena_1500, from_program=False),
       covers_mod.COVER_UPGRADE)

print("\n=== el registro no pisa lo que apunta otro proceso ===")
_carpeta_m = Path(tempfile.mkdtemp())
_a, _b, _c = (_carpeta_m / n for n in ("a.mp3", "b.mp3", "c.mp3"))
for _f in (_a, _b, _c):
    _f.write_bytes(b"audio " + _f.name.encode())
_ruta_m = _carpeta_m / "portadas.json"
_uno = covers_mod.CoverLedger(_ruta_m)
_dos = covers_mod.CoverLedger(_ruta_m)
_uno.record(_a, _img_prov, "deezer")
_dos.record(_b, _img_prov, covers_mod.MANUAL_SOURCE)
_uno.flush()
_dos.flush()
_tres = covers_mod.CoverLedger(_ruta_m)
check_that("se guardan las dos, aunque las apuntaran procesos distintos",
           str(_a) in _tres.data and str(_b) in _tres.data)
_viejo = covers_mod.CoverLedger(_ruta_m)
_otro = covers_mod.CoverLedger(_ruta_m)
_otro.record(_c, _img_prov, covers_mod.MANUAL_SOURCE)
_otro.flush()
_viejo.record(_a, _img_prov, "itunes")
_viejo.flush()
_final = covers_mod.CoverLedger(_ruta_m)
equals("una elegida a mano que apunto otro proceso sobrevive",
       (_final.data.get(str(_c)) or {}).get("fuente"), covers_mod.MANUAL_SOURCE)
equals("y el cambio propio tambien queda", _final.data[str(_a)]["fuente"], "itunes")
_ruta_m.write_text("{roto", encoding="utf-8")
_viejo.record(_b, _img_prov, "deezer")
_viejo.flush()
check_that("si el archivo esta roto no se pierde lo que habia en memoria",
           str(_c) in covers_mod.CoverLedger(_ruta_m).data)

print("\n=== la marca de la portada sigue al archivo renombrado ===")
from tagger.renaming import apply_renames, undo as deshacer_renombrados

_carpeta_r = Path(tempfile.mkdtemp())
_orig = _carpeta_r / "Cult Of Personality(MP3_128K).mp3"
_orig.write_bytes(b"audio")
_reg_r = covers_mod.CoverLedger(_carpeta_r / "portadas.json")
_reg_r.record(_orig, _img_prov, covers_mod.MANUAL_SOURCE)
_reg_r.flush()
_plan = plan_renames([_orig])
equals("el nombre se limpia", [t.name for _, t in _plan],
       ["Cult Of Personality.mp3"])
_hechos, _log, _ = apply_renames(_plan, _carpeta_r / "logs",
                                 on_rename=_reg_r.move)
_reg_r.flush()
_nuevo = _carpeta_r / "Cult Of Personality.mp3"
_releido_r = covers_mod.CoverLedger(_carpeta_r / "portadas.json")
equals("la marca 'elegida a mano' va con el archivo",
       (_releido_r.data.get(str(_nuevo)) or {}).get("fuente"),
       covers_mod.MANUAL_SOURCE)
check_that("y en la ruta vieja ya no queda", str(_orig) not in _releido_r.data)
check_that("sigue dandose por hecha: renombrar no cambia la fecha",
           _releido_r.already_done(_nuevo, _img_prov))
deshacer_renombrados(_log, on_rename=_releido_r.move)
_releido_r.flush()
equals("y al deshacer vuelve con el",
       (covers_mod.CoverLedger(_carpeta_r / "portadas.json").data.get(
           str(_orig)) or {}).get("fuente"),
       covers_mod.MANUAL_SOURCE)

print("\n=== un solo proceso escribiendo a la vez ===")
import subprocess
from tagger.candado import Candado

_ruta_c = Path(tempfile.mkdtemp()) / "en-uso.lock"
_orden_c = [sys.executable, "-c",
            "import sys; sys.path.insert(0, %r)\n"
            "from tagger.candado import Candado, BibliotecaOcupada\n"
            "try:\n    Candado(%r).adquirir()\n"
            "except BibliotecaOcupada:\n    sys.exit(3)\n"
            % (str(Path(__file__).resolve().parent.parent), str(_ruta_c))]
_primero = Candado(_ruta_c)
_primero.adquirir()
equals("otro proceso no puede entrar mientras uno escribe",
       subprocess.run(_orden_c).returncode, 3)
_primero.soltar()
equals("en cuanto termina, si", subprocess.run(_orden_c).returncode, 0)

print("\n=== ruido de los conversores de YouTube ===")
from tagger.filenames import parse_filename
from tagger.text import is_placeholder, strip_video_id

for _sucio, _limpio in (
        ("Cult Of Personality(MP3_128K)", "Cult Of Personality"),
        ("XXXTENTACION - changes(MP3_160K)", "XXXTENTACION - changes"),
        ("MC Fioti - Bum Bum Tam Tam (KondZilla)", "MC Fioti - Bum Bum Tam Tam"),
        ("La Buena y la Mala_1S9nW8gsjoY", "La Buena y la Mala"),
        ("Tatuajes_Y_KheQyXNeU", "Tatuajes"),
        ("Silvestre Dangond - Las Locuras Mías _LqKscPDW8Y0",
         "Silvestre Dangond - Las Locuras Mías"),
        ("SnapSave.io - Silent Hill 2 OST - Theme Of Laura",
         "Silent Hill 2 OST - Theme Of Laura"),
        ("Ezequiel Llanos Roze Mentiras Remix Video Oficial",
         "Ezequiel Llanos Roze Mentiras Remix"),
        ("Juan Magán Donde Estás Audio Oficial", "Juan Magán Donde Estás"),
        ("WOS ARRANCARMELO Letra Video Oficial", "WOS ARRANCARMELO"),
        ("Young Miko Riri LetraLyrics", "Young Miko Riri"),
        ("alex ponce plan visualizer", "alex ponce plan"),
        ("Dua Lipa - New Rules (Oficial Audio)", "Dua Lipa - New Rules"),
        ("Flo Rida - Whistle [Oficial Audio HD]", "Flo Rida - Whistle"),
        ("La Santa - Reykon el Líder [Canción Oficial]",
         "La Santa - Reykon el Líder"),
        ("CAMA Y MESA - ROBERTO CARLOS (FULL AUDIO)",
         "CAMA Y MESA - ROBERTO CARLOS"),
        ("Justin Bieber - Love Yourself (Audio Only)",
         "Justin Bieber - Love Yourself"),
        ("BAD OMENS - Just Pretend (Official Audio Stream)",
         "BAD OMENS - Just Pretend")):
    equals("nombre: %s" % _sucio, clean_name(_sucio), _limpio)

for _sucio, _limpio in (
        ("X2Download.com - Anitta - Envolver (Letra/Lyrics)", "Anitta - Envolver"),
        ("y2meta.com - Burak Yeter - Tuesday (Official Music Video)",
         "Burak Yeter - Tuesday"),
        ("Tati Zaqui e PK - Escandalosa (kondzilla.com)",
         "Tati Zaqui e PK - Escandalosa"),
        ("¿Y qué pasó? www.jordanoficial.com", "¿Y qué pasó?"),
        ("El Cangri.com", "El Cangri.com")):
    equals("tag: %s" % _sucio, strip_noise(_sucio), _limpio)

check_that("un artista que es solo una web no es un artista",
           is_placeholder("artist", "SnapSave.io"))
check_that("un titulo que es solo una web tampoco",
           is_placeholder("title", "X2Download.com"))
check_that("pero un disco con .com en el nombre si es un dato",
           not is_placeholder("album", "El Cangri.com"))
equals("el id tampoco llega al titulo que se deduce del nombre",
       parse_filename(Path("Digimon - Opening _WmGy9CAap0w.mp3"))[:2],
       ("Digimon", "Opening"))
for _palabra in ("Tema_Unstoppable", "Andy Rivera - La Oficial"):
    equals("no es un id de YouTube: %s" % _palabra,
           strip_video_id(_palabra), _palabra)
equals("'La Oficial' es un titulo, no ruido",
       clean_name("Andy Rivera - La Oficial"), "Andy Rivera - La Oficial")
equals("y un 'Oficial' suelto al final tampoco se toca",
       clean_name("La Noche - Que Nadie Se Entere Oficial"),
       "La Noche - Que Nadie Se Entere Oficial")
equals("un rango de anos no se parte al quitar el ruido",
       clean_name("MARIA JUANA _IDILIO_ Morenada 2010-2012(MP3_128K)"),
       "MARIA JUANA _IDILIO_ Morenada 2010-2012")
equals("pero un ano de subida detras de un guion si se quita",
       clean_name("Apache 207 - Kein Problem - 2019"), "Apache 207 - Kein Problem")

print("\n=== correcciones.json: todos los campos, por nombre o por ruta ===")
from tagger.correcciones import Correcciones

_dir_corr = Path(tempfile.mkdtemp())
_json_corr = _dir_corr / "correcciones.json"
_json_corr.write_text(json.dumps({
    "Eva.mp3": {"artist": "Vintage", "title": "Eva"},
    "latino\\brasil\\Eva.mp3": {"artist": "Otra", "title": "Eva", "album": "Disco"},
    "Quimbara.mp3": {"artist": "Celia Cruz", "album": "Quimbara", "date": "1974",
                     "genre": "Salsa", "portada": "https://ejemplo.com/q.jpg"},
}, ensure_ascii=False), encoding="utf-8")
_corr = Correcciones(_json_corr)
equals("por nombre", _corr.para(Path(r"D:\Musica\pop\Eva.mp3")).get("artist"),
       "Vintage")
equals("la ruta gana al nombre cuando el nombre se repite",
       _corr.para(Path(r"D:\Musica\latino\brasil\Eva.mp3")).get("artist"), "Otra")
equals("admite album, fecha, genero y portada",
       sorted(_corr.para(Path(r"D:\Musica\latino\salsa\Quimbara.mp3"))),
       ["album", "artist", "date", "genre", "portada"])
equals("un archivo sin correccion no tiene nada",
       _corr.para(Path(r"D:\Musica\x.mp3")), {})
check_that("al renombrar, la correccion va con el archivo",
           _corr.mover(Path(r"D:\Musica\latino\brasil\Eva.mp3"),
                       Path(r"D:\Musica\latino\brasil\Eva (Remix).mp3")))
_corr.guardar()
_releida = Correcciones(_json_corr)
equals("y se guarda con el nombre nuevo",
       _releida.para(Path(r"D:\Musica\latino\brasil\Eva (Remix).mp3")).get("artist"),
       "Otra")
equals("en la ruta vieja queda solo la correccion por nombre",
       _releida.para(Path(r"D:\Musica\latino\brasil\Eva.mp3")).get("artist"),
       "Vintage")

_antes_corr = pipeline.CORRECTIONS_PATH
pipeline.CORRECTIONS_PATH = _json_corr
_ruta_q = _dir_corr / "Quimbara.mp3"
_ruta_q.write_bytes(b"audio")
_crudo_q = {"artist": "salsa-merengue", "title": "Quimbara", "album": "",
            "albumartist": "salsa-merengue", "date": "2015", "genre": "Latin",
            "tracknumber": ""}
_lectura_q = tags_mod._read_tags_uncached
tags_mod._read_tags_uncached = lambda _r: dict(_crudo_q)
tags_mod.clear_caches()
try:
    _prop_q, _ = pipeline.analyze(
        _ruta_q, _dir_corr,
        mb_client=_FuenteFecha("Otro Disco", "2019-01-01", "alta"), verify=True)
finally:
    tags_mod._read_tags_uncached = _lectura_q
    tags_mod.clear_caches()
    pipeline.CORRECTIONS_PATH = _antes_corr
_escritas = {p.field: p.new for p in _prop_q if p.writable()}
equals("la correccion pone el artista", _escritas.get("artist"), "Celia Cruz")
equals("y el album que dijo la persona, no el de la fuente",
       _escritas.get("album"), "Quimbara")
equals("y la fecha, aunque la fuente diga otra", _escritas.get("date"), "1974")
equals("y el genero", _escritas.get("genre"), "Salsa")
equals("el albumartist sigue al artista corregido",
       _escritas.get("albumartist"), "Celia Cruz")
check_that("ninguna otra propuesta pisa lo corregido",
           all(p.source == "correccion manual" for p in _prop_q
               if p.field in ("artist", "album", "date", "genre")))

print("\n=== fichas revisadas a mano ===")
_dir_vm = Path(tempfile.mkdtemp())
_vm = VerificationLedger(_dir_vm / "verificadas.json")
_pista_vm = _dir_vm / "Tema (MP3_128K).mp3"
_ficha = {"artist": "Alguien", "title": "Tema", "album": "", "albumartist": "",
          "date": "", "genre": "", "tracknumber": ""}
check_that("al principio no esta fijada", not _vm.is_manual(_pista_vm, _ficha))
_vm.mark_manual(_pista_vm, _ficha)
check_that("fijada aunque le falte el album", _vm.is_manual(_pista_vm, _ficha))
check_that("y cuenta como verificada", _vm.is_verified(_pista_vm, _ficha))
check_that("si alguien cambia un tag, deja de valer",
           not _vm.is_manual(_pista_vm, dict(_ficha, title="Otro")))
_vm.data[str(_pista_vm)]["version"] = -1
check_that("no caduca al subir la version de las reglas",
           _vm.is_manual(_pista_vm, _ficha))
_nueva_vm = _dir_vm / "Tema.mp3"
_vm.move(_pista_vm, _nueva_vm, _ficha)
check_that("al renombrar sigue fijada con el nombre nuevo",
           _vm.is_manual(_nueva_vm, _ficha))
check_that("y con el nombre viejo ya no", not _vm.is_manual(_pista_vm, _ficha))
_cambiada = dict(_ficha, title="Cambiado")
_vm.move(_nueva_vm, _pista_vm, _cambiada)
check_that("si los tags cambiaron por el camino, la marca no se traslada",
           not _vm.is_manual(_pista_vm, _cambiada))

print("\n=== desempate: la fuente elige entre lo que ya habia ===")
_dir_des = Path(tempfile.mkdtemp())


class _FuenteDesempate:
    """Una sola fuente, con coincidencia que por si sola quedaria en revisar."""

    consensus_mode = True

    def __init__(self, artista, titulo, duracion=200.0, album="Disco"):
        self.respuesta = {"artist": artista, "title": titulo, "album": album,
                          "date": "2020-01-01", "genres": [],
                          "source": "prueba", "confidence": "revisar",
                          "agree": 1, "duration": duracion}

    def consensus_lookup(self, *args, **kwargs):
        return dict(self.respuesta)


def _desempatar(nombre, artista, titulo, fuente, duracion=200.0):
    ruta = _dir_des / nombre
    ruta.write_bytes(b"audio")
    crudo = {"artist": artista, "title": titulo, "album": "", "albumartist": "",
             "date": "", "genre": "", "tracknumber": ""}
    lectura, basica = tags_mod._read_tags_uncached, tags_mod.read_basic_audio_info
    tags_mod._read_tags_uncached = lambda _r: dict(crudo)
    tags_mod.read_basic_audio_info = lambda _r: {"duration": duracion,
                                                 "bitrate": 0, "size": 0}
    tags_mod.clear_caches()
    try:
        propuestas, _nota = pipeline.analyze(ruta, _dir_des, mb_client=fuente,
                                             verify=True)
    finally:
        tags_mod._read_tags_uncached = lectura
        tags_mod.read_basic_audio_info = basica
        tags_mod.clear_caches()
    finales = {"artist": artista, "title": titulo}
    for propuesta in propuestas:
        if propuesta.writable() and propuesta.field in finales:
            finales[propuesta.field] = propuesta.new
    return finales["artist"], finales["title"]


equals("campos cambiados: la fuente los pone en su sitio",
       _desempatar("Camilo - El Mismo Aire.mp3", "El Mismo Aire", "Camilo",
                   _FuenteDesempate("Camilo", "El Mismo Aire")),
       ("Camilo", "El Mismo Aire"))
equals("un canal de artista cede ante el artista que dice el nombre",
       _desempatar("Nickelback - Burn It to the Ground.mp3",
                   "Nickelback Universe", "Burn It to the Ground",
                   _FuenteDesempate("Nickelback", "Burn It to the Ground")),
       ("Nickelback", "Burn It to the Ground"))
_sin_separador = _desempatar(
    "Michelle Maciel Eden Muñoz CCC Video Oficial.mp3", "", "",
    _FuenteDesempate("Michelle Maciel & Eden Muñoz", "CCC"))
check_that("un nombre sin separador que es artista mas titulo se reparte",
           "Michelle Maciel" in _sin_separador[0] and _sin_separador[1] == "CCC",
           repr(_sin_separador))
equals("un artista que ya respalda el nombre no se cambia",
       _desempatar("Bronco feat. Manzanero - Adoro.mp3", "Bronco", "Adoro",
                   _FuenteDesempate("Manzanero", "Adoro")),
       ("Bronco", "Adoro"))
equals("con otra duracion no hay desempate",
       _desempatar("Camilo - El Mismo Aire.mp3", "El Mismo Aire", "Camilo",
                   _FuenteDesempate("Camilo", "El Mismo Aire", duracion=260.0)),
       ("El Mismo Aire", "Camilo"))
equals("un artista que no aparece en ningun sitio no se inventa",
       _desempatar("Quimbara.mp3", "", "Quimbara",
                   _FuenteDesempate("Celia Cruz", "Quimbara"))[0], "")
equals("un remix no se queda con el artista de la version original",
       _desempatar("Artista Tema Remix.mp3", "", "",
                   _FuenteDesempate("Artista", "Tema"))[0], "")

print("\n=== foto del artista, solo si es sin duda el mismo ===")


class _RespuestaJson:
    ok = True
    status_code = 200

    def __init__(self, cuerpo):
        self.cuerpo = cuerpo

    def json(self):
        return self.cuerpo


class _SesionDeezer:
    def __init__(self, rutas):
        self.rutas = rutas

    def get(self, url, timeout=None):
        for trozo, cuerpo in self.rutas.items():
            if trozo in url:
                return _RespuestaJson(cuerpo)
        return _RespuestaJson({"data": []})

    def close(self):
        pass


def _cliente_fotos(artistas, temas=()):
    cliente = deezer.DeezerClient(Path(tempfile.mkdtemp()) / "deezer.json")
    cliente._wait = lambda: None
    cliente.session = _SesionDeezer({
        "/search/artist": {"data": artistas},
        "/top": {"data": [{"title": t} for t in temas]},
    })
    return cliente


_foto = "https://cdn-images.dzcdn.net/images/artist/abc123/1000x1000-000000-80-0-0.jpg"
_charles = {"id": 7, "name": "Charles Ans", "picture_xl": _foto, "nb_fan": 50}
equals("con otra cancion suya entre sus temas, se acepta",
       _cliente_fotos([_charles], ["Olvidemos el Reloj"]).artist_picture(
           "Charles Ans", ["Olvidemos el Reloj"]), _foto)
equals("un homonimo sin ninguna cancion en comun no",
       _cliente_fotos([_charles], ["Otra Cosa"]).artist_picture(
           "Charles Ans", ["Olvidemos el Reloj"]), "")
equals("sin canciones con que comprobarlo, uno solo y con poco publico no",
       _cliente_fotos([_charles]).artist_picture("Charles Ans"), "")
equals("pero uno solo y conocido si",
       _cliente_fotos([dict(_charles, nb_fan=900000)]).artist_picture(
           "Charles Ans"), _foto)
equals("la imagen generica de Deezer no vale nunca",
       _cliente_fotos([dict(_charles, nb_fan=900000,
                            picture_xl=_foto.replace("abc123", ""))]).artist_picture(
           "Charles Ans"), "")
equals("la foto tambien se pide a 1500", covers_mod.url_en_grande(_foto),
       _foto.replace("1000x1000", "1500x1500"))

import subprocess as _sp

try:
    import imageio_ffmpeg
    _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _ffmpeg = None

print("\n=== MP4 con extension .mp3: se convierte a MP3 de verdad ===")
from tagger import conversion

_dir_conv = Path(tempfile.mkdtemp())
(_dir_conv / "vacio.mp3").write_bytes(b"")
check_that("un archivo de 0 bytes se detecta",
           conversion.esta_vacio(_dir_conv / "vacio.mp3"))
if _ffmpeg is None:
    print("  (sin ffmpeg en el venv: se salta la conversion)")
else:
    from mutagen.mp3 import MP3 as _MP3
    from mutagen.mp4 import MP4, MP4Cover

    _m4a = _dir_conv / "tmp.m4a"
    _sp.run([_ffmpeg, "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=3", "-c:a", "aac",
             "-b:a", "96k", str(_m4a)], check=True)
    _mp4 = MP4(_m4a)
    _mp4["\xa9nam"] = ["Tema Disfrazado"]
    _mp4["\xa9ART"] = ["Alguien"]
    _mp4["\xa9alb"] = ["Disco"]
    _mp4["covr"] = [MP4Cover(_portada(4, lado=600),
                             imageformat=MP4Cover.FORMAT_JPEG)]
    _mp4.save()
    _falso = _dir_conv / "Tema Disfrazado.mp3"
    _m4a.replace(_falso)
    check_that("se detecta el MP4 con extension .mp3",
               conversion.es_mp4_disfrazado(_falso))
    tags_mod.clear_caches()
    _copia, _error = conversion.convertir(_falso, _dir_conv / "copias")
    check_that("se convierte sin error", _copia is not None, _error)
    check_that("ahora es MP3 de verdad", not conversion.es_mp4_disfrazado(_falso))
    check_that("y dura lo mismo", abs(_MP3(_falso).info.length - 3.0) < 1.0)
    tags_mod.clear_caches()
    _tags_conv = tags_mod.read_tags(_falso) or {}
    equals("conserva el titulo", _tags_conv.get("title"), "Tema Disfrazado")
    equals("y el artista", _tags_conv.get("artist"), "Alguien")
    check_that("y la portada", covers_mod.same_picture(
        (tags_mod.read_audio_info(_falso) or {}).get("cover_data"),
        _portada(4, lado=600)))
    check_that("el original queda guardado tal cual",
           bool(_copia) and _copia.read_bytes()[4:8] == b"ftyp")

    print("\n=== sin portada del tema ni del disco, la foto del artista ===")
    _raiz_h = Path(tempfile.mkdtemp())
    (_raiz_h / "pop").mkdir()

    def _mp3_real(ruta, artista, titulo, album, portada=None):
        _sp.run([_ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=330:duration=2", "-c:a", "libmp3lame",
                 "-q:a", "9", str(ruta)], check=True)
        tags_mod.write_tags(ruta, {"artist": artista, "title": titulo,
                                   "album": album})
        if portada:
            tags_mod.embed_cover(ruta, portada)

    _donante = _raiz_h / "pop" / "Alguien - Otra.mp3"
    _hueco = _raiz_h / "pop" / "Alguien - Tema.mp3"
    _hueco_sin_foto = _raiz_h / "pop" / "Alguien - Tercera.mp3"
    _mp3_real(_donante, "Alguien", "Otra", "Disco Viejo", _portada(5, lado=600))
    _mp3_real(_hueco, "Alguien", "Tema", "Disco Nuevo")
    _mp3_real(_hueco_sin_foto, "Alguien", "Tercera", "Disco Tercero")
    _foto_bytes = _portada(6, lado=1000)
    _pedidas = []
    tags_mod.clear_caches()
    _reg_h = covers_mod.CoverLedger(_raiz_h / "portadas.json")
    _puestas_h, _fallos_h, _fotos_h = main_mod.heredar_portadas(
        [_donante, _hueco], _raiz_h, set(), [], [str(_hueco)], None,
        ledger=_reg_h,
        foto_artista=lambda nombre: _pedidas.append(nombre) or _foto_bytes)
    check_that("antes que la caratula de OTRO disco del artista, su foto",
               covers_mod.same_picture(
                   (tags_mod.read_audio_info(_hueco) or {}).get("cover_data"),
                   _foto_bytes))
    equals("queda apuntada como foto del artista", _reg_h.fuente(_hueco),
           covers_mod.ARTIST_PHOTO_SOURCE)
    equals("se busco por el artista", _pedidas, ["Alguien"])
    equals("y se cuenta", _fotos_h, 1)
    main_mod.heredar_portadas(
        [_donante, _hueco_sin_foto], _raiz_h, set(), [], [str(_hueco_sin_foto)],
        None, ledger=_reg_h, foto_artista=lambda nombre: None)
    check_that("sin foto, se hereda como siempre la del mismo artista",
               covers_mod.same_picture(
                   (tags_mod.read_audio_info(_hueco_sin_foto) or {}).get("cover_data"),
                   _portada(5, lado=600)))

print("\n=== portada declarada en correcciones.json ===")
_img_decl = Path(tempfile.mkdtemp()) / "elegida.jpg"
_img_decl.write_bytes(_portada(7, lado=800))
_datos_decl, _error_decl = main_mod._portada_declarada(str(_img_decl))
check_that("se lee una imagen local", covers_mod.same_picture(
    _datos_decl, _portada(7, lado=800)), _error_decl)
_no_img = _img_decl.with_name("nota.txt")
_no_img.write_text("hola", encoding="utf-8")
check_that("lo que no es una imagen se rechaza",
           main_mod._portada_declarada(str(_no_img))[0] is None)

print("\n%s\n%d correctas, %d fallos\n%s" % ("=" * 46, passed, failed, "=" * 46))
sys.exit(1 if failed else 0)

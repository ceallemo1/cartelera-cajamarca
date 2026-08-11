# -*- coding: utf-8 -*-
"""
Bloque: Cines (Cajamarca), con doble indice. El codigo sigue siendo multi-ciudad
-- basta agregar una entrada a CIUDADES --, pero solo Cajamarca esta activa.

Fuente: carteleracine.pe (sin API key). Misma mecanica para toda ciudad:

  1) LISTADO  /cartelera/<ciudad>-c<ID>   -> titulo + slug + poster de cada peli.
  2) DETALLE  /<ciudad>/<slug>_<ID>       -> por cada CINE: nombre, direccion,
     formato(s) y horarios reales. La ficha agrupa en bloques <div class="caja-cinema">
     (en Cajamarca son 3 cines por peli como mucho). Tambien metadatos de la
     peli: genero, duracion, clasificacion, director, actores, estreno.
     (Todo esto SE PARSEA del HTML: estable y fiable, no se delega al LLM.)
  3) ENRIQUECER (LLM gratis, opcional): sinopsis breve + puntaje (0-10). El LLM
     SOLO rellena lo que conoce; si no, deja el campo vacio. Nada se inventa.
     trailer_url = enlace de BUSQUEDA YouTube por titulo.

DOBLE INDICE en la salida, para la mini-app del tablero:
  - "peliculas": lista por pelicula; cada una con sus cines+formatos+horarios.
  - "cines": indice invertido por cine; cada cine con las pelis (y horarios) que da.

La pagina NO publica precios -> no hay precio (no se inventa).
El HTML viene en ISO-8859-1: se decodifica con fallback utf-8 -> latin-1.

Lo corre un cron de OpenClaw (brazo ejecutor desde el 2026-08-02, ADR-041; antes
era Hermes, retirado). La pagina del tablero solo LEE el JSON, no scrapea nada.
Salida por ciudad: D:/Tools/Tablero_cache/cines_<ciudad>.json
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from motor_llm import llamar  # noqa: E402
try:
    from tmdb import enriquecer_tmdb, disponible as _tmdb_ok  # noqa: E402
except Exception:  # TMDb es opcional: si falta el modulo, el bloque sigue igual
    enriquecer_tmdb = None
    def _tmdb_ok():
        return False

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
BASE = "https://www.carteleracine.pe"
# Carpeta del cache. En la laptop es la de siempre; en la nube (GitHub Actions) no
# existe la unidad D:, asi que el runner la pasa por TABLERO_CACHE. El default deja
# el comportamiento local EXACTAMENTE igual que antes.
CACHE_DIR = os.environ.get("TABLERO_CACHE", "D:/Tools/Tablero_cache")
PERU = timezone(timedelta(hours=-5))
MAX_PELIS = 16

# Ciudades soportadas: slug de URL + id de ciudad + etiqueta legible.
# CAJAMARCA Y NADA MAS (decision de Cesar, 2026-08-03): "ya no quiero los cines
# de Lima". Lima salio del bloque entero -- scraper, cache, verificador y UI --,
# no solo de la pantalla. Si volviera algun dia, su id en carteleracine es 245.
CIUDADES = {
    "cajamarca": {"id": "252", "etiqueta": "Cajamarca"},
}


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("iso-8859-1", "ignore")  # carteleracine sirve latin-1


def _texto(html):
    """Aplana HTML a texto: quita SVG, tags, normaliza entidades y espacios."""
    html = re.sub(r"<svg.*?</svg>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("\xa0", " ")
            .replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", html).strip()


def _to24h(h):
    """'4:00 pm' -> '16:00'. Deja intacto lo que ya viene en 24h."""
    h = h.strip().lower()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", h)
    if not m:
        return h.replace(" ", "")
    hh, mm, ap = int(m.group(1)), m.group(2), m.group(3)
    if ap == "pm" and hh != 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    return f"{hh:02d}:{mm}"


def _campos_li(html):
    """Lee los <b>Etiqueta</b>: Valor de la ficha (Genero, Duracion, etc.)."""
    out = {}
    for m in re.finditer(r"<b>([^<]+)</b>\s*:?\s*([^<]+)", html):
        k = _texto(m.group(1)).rstrip(":").strip().lower()
        v = _texto(m.group(2)).strip(":").strip()
        if v and len(v) < 120:
            out[k] = v
    return out


def _cines_de_ficha(html):
    """Extrae la lista de cines de una ficha de pelicula.

    Cada cine = un bloque <div class="caja-cinema"> con:
      <h2 class="nombre-cinema"><a href="/<slug-cine>">NOMBRE</a></h2>
      <div class="datos-cinema">direccion</div>
      (luego, intercalados) <div class="formato-pelicula">FORMATO</div>
                            <div class="horarios-funcion"><ul><li>HH:MM am/pm</li>...</ul></div>
    Un cine puede repetir formato+horarios (2D doblada, 2D subtitulada, etc.).
    Devuelve [{cine, cine_slug, direccion, formatos:[{formato, horarios:[]}],
               horarios:[] (union ordenada)}].
    """
    cines = []
    # Trocea por bloque de cine. Cada bloque va de un 'caja-cinema' al siguiente.
    bloques = re.split(r'<div class="caja-cinema"', html)
    for b in bloques[1:]:
        mn = re.search(r'nombre-cinema[^>]*>\s*<a href="/([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not mn:
            continue
        cine_slug = mn.group(1).strip()
        cine = _texto(mn.group(2))
        md = re.search(r'datos-cinema[^>]*>(.*?)</div>', b, re.S)
        direccion = _texto(md.group(1)) if md else ""
        # Pares formato -> horarios. Recorremos en orden de aparicion.
        formatos = []
        union = []
        # Cada formato seguido (en el HTML) por su bloque de horarios.
        for mf in re.finditer(
            r'formato-pelicula[^>]*>(.*?)</div>\s*<div class="horarios-funcion">(.*?)</div>',
            b, re.S,
        ):
            fmt = _texto(mf.group(1)).strip(" -")
            hrs = []
            for hr in re.findall(r"\d{1,2}:\d{2}\s*[ap]m", mf.group(2), re.I):
                h24 = _to24h(hr)
                if h24 not in hrs:
                    hrs.append(h24)
            hrs.sort()
            if hrs:
                formatos.append({"formato": fmt, "horarios": hrs})
                for h in hrs:
                    if h not in union:
                        union.append(h)
        union.sort()
        if not formatos:
            continue
        cines.append({
            "cine": cine,
            "cine_slug": cine_slug,
            "direccion": direccion,
            "formatos": formatos,
            "horarios": union,
        })
    return cines


def _detalle(ciudad_slug, ciudad_id, slug, titulo, poster):
    """Baja la ficha de la ciudad para una pelicula: cines+horarios + metadatos.
    Si la ficha no carga, devuelve lo minimo (titulo/poster)."""
    peli = {
        "titulo": titulo,
        "poster": poster,
        "cines": [],
        "genero": "",
        "duracion": "",
        "clasificacion": "",
        "director": "",
        "actores": "",
        "estreno": "",
        "ficha": f"{BASE}/{ciudad_slug}/{slug}_{ciudad_id}",
        "trailer_url": "https://www.youtube.com/results?search_query="
                       + urllib.parse.quote(titulo + " trailer espanol"),
    }
    try:
        html = _fetch(peli["ficha"])
    except Exception:
        return peli
    peli["cines"] = _cines_de_ficha(html)
    li = _campos_li(html)
    peli["genero"] = li.get("género", li.get("genero", ""))
    peli["duracion"] = li.get("duración", li.get("duracion", ""))
    peli["clasificacion"] = li.get("clasificación", li.get("clasificacion", ""))
    peli["director"] = li.get("director", "")
    ma = re.search(r"Actores:\s*</b>\s*([^<]+)", html) or re.search(r"Actores:\s*([^<]+)", html)
    if ma:
        peli["actores"] = _texto(ma.group(1))[:160]
    me = re.search(r"Fecha de estreno\s*([A-Za-z]{3}\s*\d{1,2}\s*/\s*\d{4})", html)
    if me:
        peli["estreno"] = _texto(me.group(1))
    return peli


def _listar(html):
    """Del listado de cartelera saca (slug, titulo, poster) de cada pelicula."""
    items = []
    vistos = set()
    # Captura el <a href> de cada titulo. El titulo se toma de title="..." si existe;
    # si no (algunas pelis no traen el atributo, ej. Supergirl), se usa el texto del
    # enlace. Asi no se pierden peliculas validas.
    titulos = list(re.finditer(
        r'<h2 class="title-pelicula[^"]*"[^>]*>\s*<a href="/([a-z0-9-]+)"([^>]*)>(.*?)</a>',
        html, re.S,
    ))
    for i, m in enumerate(titulos):
        slug, attrs, inner = m.group(1), m.group(2), m.group(3)
        mt = re.search(r'title="([^"]+)"', attrs)
        titulo = _texto(mt.group(1)) if mt else _texto(inner)
        if not titulo:
            continue
        if slug in vistos:
            continue
        vistos.add(slug)
        # La ventana de busqueda se ACOTA al siguiente titulo. Sin ese tope, una
        # pelicula cuya tarjeta no trae imagen a la derecha se lleva la de la
        # tarjeta siguiente (paso con la 1ra: Spider-Man salia con el webp de La
        # Odisea, 2026-08-03). Medido en el HTML real: las tarjetas 2..N ponen su
        # <picture> DESPUES del titulo (lazy, data-src), pero la PRIMERA carga
        # eager y su <img src> queda ANTES. Por eso hacen falta las dos miradas.
        fin = titulos[i + 1].start() if i + 1 < len(titulos) else m.end() + 2500
        resto = html[m.end():fin]
        mp = re.search(r'data-srcset="(https://cdn\.carteleracine\.pe/[^"]+?\.(?:jpg|webp))"', resto)
        if not mp:
            mp = re.search(r'data-src="(https://cdn\.carteleracine\.pe/[^"]+)"', resto)
        if not mp:
            # Rescate hacia ATRAS (tarjeta eager): desde el titulo anterior hasta
            # este. Se toma la ULTIMA coincidencia porque es la mas pegada a ESTE
            # titulo, o sea la de su propia tarjeta.
            ini = titulos[i - 1].end() if i else 0
            previos = re.findall(
                r'(?:data-src|src)="(https://cdn\.carteleracine\.pe/[^"]+?\.(?:jpg|webp))"',
                html[ini:m.start()],
            )
            if previos:
                mp = previos[-1]
        poster = (mp if isinstance(mp, str) else mp.group(1)) if mp else ""
        items.append((slug, titulo, poster))
        if len(items) >= MAX_PELIS:
            break
    return items


def _indice_por_cine(pelis):
    """Invierte: de pelis->cines a cines->pelis. Cada cine con las pelis que da
    (titulo, poster, formatos, horarios)."""
    cines = {}
    for p in pelis:
        for c in p.get("cines", []):
            slug = c["cine_slug"]
            if slug not in cines:
                cines[slug] = {
                    "cine": c["cine"],
                    "cine_slug": slug,
                    "direccion": c.get("direccion", ""),
                    "peliculas": [],
                }
            cines[slug]["peliculas"].append({
                "titulo": p["titulo"],
                "poster": p.get("poster", ""),
                "genero": p.get("genero", ""),
                "clasificacion": p.get("clasificacion", ""),
                "formatos": c.get("formatos", []),
                "horarios": c.get("horarios", []),
            })
    # ordena alfabeticamente por nombre de cine
    return sorted(cines.values(), key=lambda x: x["cine"])


def _cines_del_listado(html):
    """Los cines que la ciudad TIENE, salgan o no en la cartelera de hoy.

    El listado de ciudad trae un boton por sala, con su enlace y su nombre legible:
      <a href="/cinerama-cajamarca-megaplaza-cajamarca-s471">Cajamarca Megaplaza (Cinerama)</a>
    Se descubren asi, y no con una lista fija en el codigo, para que una sala nueva
    aparezca sola sin tener que tocar nada.
    """
    vistos, salas = set(), []
    for m in re.finditer(r'<a[^>]+href="/([a-z0-9\-]+-s\d+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        slug, nombre = m.group(1), _texto(m.group(2))
        if slug in vistos or not nombre:
            continue
        vistos.add(slug)
        salas.append({"cine_slug": slug, "cine": nombre})
    return salas


def _completar_salas_sin_funciones(cines, salas):
    """Anade las salas de la ciudad que hoy no tienen ni una funcion.

    POR QUE: los cines se deducen de las fichas de pelicula, asi que una sala sin
    programacion publicada simplemente DESAPARECE de la lista. Y una sala que
    desaparece se lee como una sala que no existe: el 2026-08-05 el tablero mostraba
    2 cines en Cajamarca cuando son 3 — Cinerama Cajamarca Megaplaza estaba con
    "no hay programacion disponible para este sala" en la fuente. Decir "sin
    programacion publicada" es un dato; omitirla es una respuesta equivocada.
    """
    ya = {c["cine_slug"] for c in cines}
    for s in salas:
        if s["cine_slug"] in ya:
            continue
        cines.append({
            "cine": s["cine"],
            "cine_slug": s["cine_slug"],
            "direccion": "",
            "peliculas": [],
            "sin_programacion": True,
            "nota": "La fuente no publica funciones de esta sala hoy.",
        })
    return sorted(cines, key=lambda x: (bool(x.get("sin_programacion")), x["cine"]))


def _norm_titulo(t):
    """Normaliza un titulo para emparejar entre fuentes (Cineplanet vs carteleracine):
    minusculas, sin acentos, sin signos, espacios colapsados."""
    t = (t or "").lower().strip()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                 ("ñ", "n"), ("ü", "u")):
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Palabras que NO identifican al local, solo a la cadena. Se descartan para
# emparejar "CAJAMARCA - CINEPLANET" con "CP Cajamarca": lo que distingue un
# Cineplanet de otro es el resto ("Alcazar", "Primavera", "Cajamarca").
_MARCA_CP = {"cineplanet", "cp", "cine", "cines", "planet", "cinema", "cinemas", "de", "del", "la", "el"}


def _es_cineplanet(nombre, slug):
    n = _norm_titulo(nombre)
    return "cineplanet" in n or str(slug or "").startswith("cineplanet-")


def _clave_local(nombre):
    """Tokens que identifican al LOCAL, sin la marca. Ordenados, como conjunto."""
    toks = [t for t in _norm_titulo(nombre).split() if t and t not in _MARCA_CP]
    return tuple(sorted(set(toks)))


def _mismo_local(nombre_cc, nombre_cp, ciudad_slug):
    """¿"ALCAZAR - CINEPLANET - LIMA" y "CP Alcazar" son el mismo cine?

    carteleracine cuelga la ciudad del nombre y Cineplanet no, asi que comparar
    los tokens tal cual solo funciona donde la ciudad ES el nombre del local
    ("CAJAMARCA - CINEPLANET" vs "CP Cajamarca"). En Lima fallaria en los 27.

    Se compara por IGUALDAD, no por subconjunto, y ahi esta el detalle que
    importa: con subconjunto, "CP Santa Clara" casaria con "SANTA CLARA QHATU
    PLAZA - CINEPLANET - LIMA" ademas de con el suyo, y deduplicar contra el
    cine equivocado borra funciones reales. La igualdad tras descontar la ciudad
    deja {santa,clara} != {santa,clara,qhatu,plaza} y no hay ambiguedad.
    """
    a = set(_clave_local(nombre_cc))
    b = set(_clave_local(nombre_cp))
    if not a or not b:
        return False   # sin nada que identifique al local, no se arriesga
    c = {_norm_titulo(ciudad_slug)}
    return a == b or (a - c) == b or a == (b - c)


def _fusionar_cineplanet(resultado, ciudad_slug, ahora):
    """Fusiona el side-cache de Cineplanet (lo deja refrescar_cineplanet.py via
    Playwright) en AMBOS indices del resultado: 'cines' y 'peliculas'.

    Cineplanet no aparece en carteleracine.pe, asi que sin esto el cine queda
    ausente del tablero. El side-cache trae cada cine ya con sus peliculas+horarios
    reales (forma identica al indice 'cines').

    Degrada en silencio (no toca nada) si el side-cache falta, esta vacio, dio
    error, o es de OTRA fecha (horarios viejos no deben mostrarse como de hoy).
    El bloque sigue siendo stdlib puro: aqui solo se lee un JSON ya generado.
    """
    cache = f"{CACHE_DIR}/cineplanet_{ciudad_slug}.json"
    try:
        with open(cache, encoding="utf-8") as f:
            cp = json.load(f)
    except Exception:
        return  # sin side-cache: el tablero muestra solo carteleracine
    if cp.get("estado") != "ok" or not cp.get("cines"):
        return
    # No mostrar horarios de otro dia como si fueran de hoy.
    if cp.get("fecha_objetivo") and cp["fecha_objetivo"] != ahora.strftime("%Y-%m-%d"):
        return

    cp_cines = cp["cines"]
    resultado.setdefault("fuentes_extra", []).append("Cineplanet")

    # --- Deduplicar ENTRE FUENTES antes de anexar nada ---------------------
    # carteleracine tambien lista los Cineplanet, con otro nombre y otro slug
    # ("CAJAMARCA - CINEPLANET" / cineplanet-cajamarca-cajamarca-s406) que el que
    # usa Cineplanet ("CP Cajamarca" / cp-cajamarca). Deduplicar por slug no los
    # cruza, asi que el tablero mostraba el MISMO local dos veces y contaba 4
    # cines en una ciudad que tiene 3 (visto el 2026-08-03).
    # Gana Cineplanet para sus propios cines: es la fuente oficial, trae el
    # trailer real, el enlace de compra y retira las funciones que ya pasaron.
    for c in cp_cines:
        # Sin nada que identifique al local ("CINEPLANET" a secas) no se puede
        # saber de que cine habla, y emparejar a ciegas borraria uno bueno al
        # azar. Ante la duda, no se deduplica: _mismo_local devuelve False.
        gemelos = [x for x in resultado.get("cines", [])
                   if x.get("fuente_cine") != "Cineplanet"
                   and _es_cineplanet(x.get("cine", ""), x.get("cine_slug", ""))
                   and _mismo_local(x.get("cine", ""), c.get("cine", ""), ciudad_slug)]
        for g in gemelos:
            slug_viejo = g.get("cine_slug")
            resultado["cines"].remove(g)
            # Y del otro indice, o los dos se contradicen (lo detecta verificar_cines.py).
            for p in resultado.get("peliculas", []):
                p["cines"] = [cc for cc in p.get("cines", [])
                              if cc.get("cine_slug") != slug_viejo]
            resultado["peliculas"] = [p for p in resultado.get("peliculas", [])
                                      if p.get("cines")]

    # --- Titulo canonico ---------------------------------------------------
    # Las dos fuentes escriben distinto la MISMA pelicula: carteleracine pone
    # "Spider Man: Un Nuevo Dia" y Cineplanet "Spider man Un nuevo dia". El
    # indice 'peliculas' ya unifica (mergea por _norm_titulo contra el titulo que
    # ya estaba), pero el indice 'cines' copiaba la grafia de Cineplanet tal cual
    # -> la misma pelicula salia con dos escrituras segun la pantalla: en la
    # lista con una y dentro de CP Cajamarca con otra. Manda el titulo que ya
    # esta en el indice global; si la pelicula solo la trae Cineplanet, se queda
    # el suyo, que es el unico que hay.
    canon = {_norm_titulo(p["titulo"]): p["titulo"] for p in resultado.get("peliculas", [])}

    def _canon(t):
        return canon.get(_norm_titulo(t), t)

    # --- Indice 'cines': anexar los cines de Cineplanet (evitar duplicar por slug) ---
    slugs = {c.get("cine_slug") for c in resultado.get("cines", [])}
    for c in cp_cines:
        if c.get("cine_slug") in slugs:
            continue
        resultado.setdefault("cines", []).append({
            "cine": c["cine"],
            "cine_slug": c.get("cine_slug", ""),
            "direccion": c.get("direccion", ""),
            "url": c.get("url", ""),
            "comprar": c.get("comprar", ""),
            "fuente_cine": "Cineplanet",
            "peliculas": [
                {
                    "titulo": _canon(p["titulo"]),
                    "poster": p.get("poster", ""),
                    "genero": p.get("genero", ""),
                    "clasificacion": p.get("clasificacion", ""),
                    "formatos": p.get("formatos", []),
                    "horarios": p.get("horarios", []),
                }
                for p in c.get("peliculas", [])
            ],
        })
    # Mismo criterio que _completar_salas_sin_funciones: las salas sin programacion
    # van al final. Un sort a secas por nombre las mezclaria entre las que si tienen.
    resultado["cines"].sort(key=lambda x: (bool(x.get("sin_programacion")), x["cine"]))

    # --- Indice 'peliculas': mergear cada peli de Cineplanet en la lista global ---
    por_titulo = {_norm_titulo(p["titulo"]): p for p in resultado.get("peliculas", [])}
    for c in cp_cines:
        cine_obj_base = {
            "cine": c["cine"],
            "cine_slug": c.get("cine_slug", ""),
            "direccion": c.get("direccion", ""),
            "url": c.get("url", ""),
            "comprar": c.get("comprar", ""),
            "fuente_cine": "Cineplanet",
        }
        for p in c.get("peliculas", []):
            cine_entry = dict(cine_obj_base, **{
                "formatos": p.get("formatos", []),
                "horarios": p.get("horarios", []),
            })
            clave = _norm_titulo(p["titulo"])
            existente = por_titulo.get(clave)
            if existente:
                existente.setdefault("cines", []).append(cine_entry)
                # completar metadatos que carteleracine no trajo (solo si faltan)
                for k in ("poster", "genero", "clasificacion", "duracion", "sinopsis"):
                    if not existente.get(k) and p.get(k):
                        existente[k] = p[k]
                # Trailer: carteleracine SIEMPRE pone un link de BUSQUEDA de YouTube
                # (no es un video). Cineplanet trae el trailer REAL. Si Cineplanet lo
                # tiene, reemplaza al link de busqueda (este es el campo que Cesar
                # reclamo: "existe un trailer?"). Se reconoce el de busqueda por la URL.
                tp = p.get("trailer_url", "")
                te = existente.get("trailer_url", "")
                es_busqueda = "results?search_query=" in te
                if tp and (es_busqueda or not te):
                    existente["trailer_url"] = tp
            else:
                nueva = {
                    "titulo": p["titulo"],
                    "poster": p.get("poster", ""),
                    "genero": p.get("genero", ""),
                    "clasificacion": p.get("clasificacion", ""),
                    "duracion": p.get("duracion", ""),
                    "director": "",
                    "actores": "",
                    "estreno": "",
                    "sinopsis": p.get("sinopsis", ""),
                    "puntaje": "",
                    "ficha": "",
                    "trailer_url": p.get("trailer_url", ""),
                    "fuente_peli": "Cineplanet",
                    "cines": [cine_entry],
                }
                por_titulo[clave] = nueva
                resultado.setdefault("peliculas", []).append(nueva)


def _enriquecer(pelis):
    """Una sola llamada LLM: sinopsis + puntaje por titulo.
    La SINOPSIS describe la premisa de la pelicula/saga (redaccion, no invencion de
    datos). El PUNTAJE solo se da si el modelo lo conoce; si no, "" (no se inventa).
    Se prioriza Gemini (mejor redaccion y no fabrica notas); cae a la cascada."""
    if not pelis:
        return None
    listado = "\n".join(f"{i+1}. {p['titulo']}" for i, p in enumerate(pelis))
    prompt = (
        "Eres un redactor de cartelera de cine. Para CADA pelicula escribe una sinopsis "
        "breve (1-2 frases, espanol neutro) que describa de que trata segun lo conocido "
        "de la pelicula o su saga. Si es secuela de una saga famosa (Toy Story, Minions, "
        "etc.) describe su premisa. Para el puntaje, da una nota 0-10 estilo IMDb SOLO si "
        "la conoces con seguridad; si no, deja \"\" (no inventes notas). Devuelve SOLO un "
        "JSON valido, una entrada por pelicula en el MISMO orden:\n"
        '{"items":[{"sinopsis":"...","puntaje":""}]}\n\n'
        "PELICULAS:\n" + listado
    )
    try:
        try:
            crudo, motor = llamar(prompt, motor="gemini", max_tokens=900)
        except Exception:
            crudo, motor = llamar(prompt, max_tokens=900)  # cascada de respaldo
    except Exception:
        return None
    m = re.search(r"\{.*\}", crudo, re.S)
    if not m:
        return motor
    try:
        items = json.loads(m.group(0)).get("items", [])
    except Exception:
        return motor
    for p, it in zip(pelis, items):
        if not isinstance(it, dict):
            continue
        sino = str(it.get("sinopsis", "") or "").strip()
        punt = str(it.get("puntaje", "") or "").strip()
        if sino:
            p["sinopsis"] = sino
        if punt:
            p["puntaje"] = punt
    return motor


# Como se llama cada sala EN PANTALLA. Regla que fijo Cesar el 2026-08-05: "la
# empresa de cine y el centro comercial donde se encuentra" — "CP Cajamarca" no dice
# ni la cadena entera ni donde esta. La clave es el nombre que llega de la fuente.
#
# Se aplica al FINAL, despues de las fusiones, y nunca antes: la deduplicacion entre
# fuentes empareja por nombre (_es_cineplanet, _mismo_local), asi que renombrar antes
# haria que el mismo local dejara de reconocerse y saliera dos veces.
NOMBRES_BONITOS = {
    "CP Cajamarca": "CinePlanet Cajamarca Real Plaza",
    "CAJAMARCA - MOVIE TIME": "Movie Time Cajamarca",
    "CAJAMARCA - CINEPLANET": "CinePlanet Cajamarca Real Plaza",
    "Cajamarca Megaplaza (Cinerama)": "Cinerama Cajamarca MegaPlaza",
}


def _renombrar_cines(resultado):
    """Pone el nombre de pantalla en los dos indices, que tienen que decir lo mismo."""
    for c in resultado.get("cines", []):
        original = (c.get("cine") or "").strip()
        bonito = NOMBRES_BONITOS.get(original)
        if bonito:
            # Se conserva el nombre de la fuente: verificar_cines.py cruza el cache
            # contra carteleracine con _mismo_local, y "CinePlanet Cajamarca Real
            # Plaza" no casa con "CAJAMARCA - CINEPLANET" (sobran 'real' y 'plaza').
            # Sin este campo, el renombrado hacia cantar fallos donde no los hay.
            c["cine_original"] = original
            c["cine"] = bonito
    for p in resultado.get("peliculas", []):
        for c in p.get("cines", []):
            bonito = NOMBRES_BONITOS.get((c.get("cine") or "").strip())
            if bonito:
                c["cine"] = bonito


def _fusionar_cinerama(resultado, ciudad_slug):
    """Mete la cartelera propia de Cinerama y retira la sala vacia del agregador."""
    import cinerama
    cine, err = cinerama.cine_de(ciudad_slug, resultado.get("fecha_objetivo"))
    if not cine:
        resultado.setdefault("notas", []).append("Cinerama: " + str(err))
        # Si el fallo fue NUESTRO —no se pudo hablar con la web—, la sala vacia que dejo
        # carteleracine se queda en pie diciendo "la fuente no publica funciones hoy", y
        # eso es MENTIRA: Cinerama funciona todos los dias en Cajamarca. Cesar lo marco
        # el 2026-08-11 tras un HTTP 500 pasajero de las 11:00. Aqui no se puede saber
        # que dan, pero si se puede dejar de afirmar lo que no se sabe: se marca la sala
        # para que quien la pinte diga "no se pudo consultar" y ofrezca su web.
        if str(err).startswith("NO_CONSULTADO"):
            conf = cinerama.SEDES.get(ciudad_slug) or {}
            for c in resultado.get("cines", []):
                if "cinerama" in ((c.get("cine_slug") or "") + (c.get("cine") or "")).lower():
                    c["no_consultado"] = True
                    c["nota"] = "No se pudo consultar su cartelera. Suele tener funciones a diario."
                    if conf.get("url"):
                        c["url"] = conf["url"]
        return

    # Fuera la ficha vacia que dejo carteleracine para esta misma sala: se reconoce
    # porque su slug empieza por 'cinerama'. Si se dejara, el tablero mostraria la
    # sala dos veces, una con funciones y otra diciendo que no tiene.
    def _es_cinerama(c):
        return ("cinerama" in (c.get("cine_slug") or "").lower()
                or "cinerama" in (c.get("cine") or "").lower())

    resultado["cines"] = [c for c in resultado.get("cines", []) if not _es_cinerama(c)]
    resultado["cines"].append(cine)
    resultado.setdefault("fuentes_extra", []).append("Cinerama")

    # Y del indice por PELICULA hay que retirarlo tambien, no solo del de cines. Hay
    # dias en que carteleracine SI publica funciones de Cinerama dentro de las fichas
    # —el 2026-08-05 lo hizo— y entonces la sala salia dos veces en la misma pelicula:
    # una como "CAJAMARCA MEGAPLAZA - CINERAMA - CAJAMARCA" y otra con su nombre bueno.
    # Manda la fuente de la cadena, igual que con Cineplanet.
    for p in resultado.get("peliculas", []):
        p["cines"] = [c for c in p.get("cines", []) if not _es_cinerama(c)]
    resultado["peliculas"] = [p for p in resultado.get("peliculas", []) if p.get("cines")]

    # Clave de emparejamiento mas tolerante que _norm_titulo a secas: Cinerama escribe
    # "SPIDERMAN: UN NUEVO DIA (PREVENTA)" y carteleracine "Spider Man: Un Nuevo Dia".
    # Sin quitar el parentesis y los espacios quedaban como dos peliculas distintas.
    def _clave_titulo(t):
        base = re.sub(r"\([^)]*\)", " ", t or "")
        return _norm_titulo(base).replace(" ", "")

    por_titulo = {_clave_titulo(p.get("titulo")): p for p in resultado.get("peliculas", [])}
    for pel in cine["peliculas"]:
        entrada = {
            "cine": cine["cine"], "cine_slug": cine["cine_slug"],
            "direccion": cine["direccion"],
            "formatos": pel["formatos"], "horarios": pel["horarios"],
        }
        ya = por_titulo.get(_clave_titulo(pel["titulo"]))
        if ya:
            # Los DOS indices tienen que llamar igual a la misma pelicula. Cinerama la
            # titula "Spiderman: Un Nuevo Dia (Preventa)" y carteleracine "Spider Man:
            # Un Nuevo Dia"; al emparejar se queda el titulo ya existente y se corrige
            # tambien dentro de la ficha del cine. Sin esto, verificar_cines.py marca
            # "indices descuadrados" con razon: el par cine/pelicula no coincide.
            pel["titulo"] = ya["titulo"]
            ya.setdefault("cines", []).append(entrada)
        else:
            nueva = {k: pel.get(k, "") for k in
                     ("titulo", "poster", "genero", "clasificacion", "duracion", "sinopsis")}
            nueva.update({"director": "", "actores": "", "estreno": "", "puntaje": "",
                          "ficha": "", "trailer_url": "", "fuente_peli": "Cinerama",
                          "cines": [entrada]})
            resultado.setdefault("peliculas", []).append(nueva)
            por_titulo[_clave_titulo(pel["titulo"])] = nueva


def _ciudad(ciudad_slug, conf, ahora):
    """Procesa una ciudad completa y devuelve su dict resultado."""
    cid = conf["id"]
    listado = f"{BASE}/cartelera/{ciudad_slug}-c{cid}"
    resultado = {
        "ciudad": ciudad_slug,
        "titulo": f"Cines en {conf['etiqueta']}",
        "fuente": listado,
        "actualizado": ahora.strftime("%Y-%m-%d %H:%M"),
        # A que DIA corresponden estos horarios. No es lo mismo que 'actualizado':
        # el cron corre a una hora fija, asi que desde medianoche hasta esa hora el
        # cache que sirve el tablero es el del dia ANTERIOR. Sin esta marca el front
        # no puede saberlo y rotula "Funciones hoy" sobre funciones de ayer (el aviso
        # de dato rancio no lo cubre: salta recien a las 48 h). El side-cache de
        # Cineplanet ya guardaba su 'fecha_objetivo'; esto le da la misma proteccion
        # a carteleracine.
        "fecha_objetivo": ahora.strftime("%Y-%m-%d"),
        "peliculas": [],
        "cines": [],
        "estado": "ok",
    }
    try:
        html_listado = _fetch(listado)
        items = _listar(html_listado)
        if not items:
            resultado["estado"] = "vacio"
            return resultado
        pelis = [_detalle(ciudad_slug, cid, slug, titulo, poster)
                 for slug, titulo, poster in items]
        for p in pelis:
            p.setdefault("sinopsis", "")
            p.setdefault("puntaje", "")
        motor = _enriquecer(pelis)
        if motor:
            resultado["motor"] = motor
        resultado["peliculas"] = pelis
        # Las salas sin funciones se completan AQUI, antes de fusionar Cineplanet:
        # si se hiciera despues, el Cineplanet de carteleracine (s406) — que la
        # fusion acaba de retirar por duplicado — volveria a entrar como sala vacia
        # y el mismo local saldria dos veces.
        resultado["cines"] = _completar_salas_sin_funciones(
            _indice_por_cine(pelis), _cines_del_listado(html_listado))
    except Exception as e:
        resultado["estado"] = "error"
        resultado["error"] = f"{type(e).__name__}: {e}"
    # Fusion de Cineplanet (side-cache via Playwright). Fuera del try anterior:
    # si carteleracine fallo pero hay side-cache, igual mostramos Cineplanet.
    try:
        _fusionar_cineplanet(resultado, ciudad_slug, ahora)
        # Si carteleracine vino vacio/error pero Cineplanet aporto cines, recuperar 'ok'.
        if resultado["estado"] != "ok" and resultado.get("cines"):
            resultado["estado"] = "ok"
    except Exception:
        pass  # la fusion nunca debe tumbar el bloque

    # Cinerama, por el mismo motivo que Cineplanet: la cadena publica su cartelera y
    # el agregador no. carteleracine LISTA la sala de Cajamarca pero dice que no tiene
    # programacion, mientras la web de Cinerama da 5 peliculas y 16 funciones el mismo
    # dia. Cesar lo detecto: "los 3 estan funcionando".
    try:
        _fusionar_cinerama(resultado, ciudad_slug)
        if resultado["estado"] != "ok" and resultado.get("cines"):
            resultado["estado"] = "ok"
    except Exception:
        pass

    _renombrar_cines(resultado)

    # TMDb: calificacion de criticos + reseñas reales por pelicula (despues de la
    # fusion, para cubrir TODAS las pelis incl. las que solo trae Cineplanet).
    # Opcional: si no hay key o el modulo falta, no toca nada y nunca tumba el bloque.
    if enriquecer_tmdb and _tmdb_ok():
        for p in resultado.get("peliculas", []):
            try:
                enriquecer_tmdb(p)
            except Exception:
                pass  # un fallo por pelicula no debe afectar a las demas
    return resultado


def main():
    ahora = datetime.now(PERU)
    # Permite correr una sola ciudad: python bloque_cines.py cajamarca
    pedidas = [a for a in sys.argv[1:] if a in CIUDADES] or list(CIUDADES)
    fallos = 0
    for ciudad_slug in pedidas:
        conf = CIUDADES[ciudad_slug]
        res = _ciudad(ciudad_slug, conf, ahora)
        cache = f"{CACHE_DIR}/cines_{ciudad_slug}.json"
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        n, nc = len(res["peliculas"]), len(res["cines"])
        if res["estado"] == "ok":
            print(f"Cines {conf['etiqueta']}: {n} peliculas, {nc} cines ({res['actualizado']}).")
        elif res["estado"] == "vacio":
            fallos += 1
            print(f"Cines {conf['etiqueta']}: cartelera vacia (revisar HTML).")
        else:
            fallos += 1
            print(f"Cines {conf['etiqueta']} ERROR: {res.get('error')}")

    # Publicar la cartelera en la web publica. Va DESPUES de escribir el cache y
    # dentro de try/except a proposito: el tablero de Cesar no puede quedarse sin
    # cartelera porque falle una subida a internet. Si el push falla, se dice y ya.
    # En la nube (GitHub Actions) este paso NO va: alli no existe el repo local de
    # D: y el commit lo hace el propio workflow sobre su checkout. Correrlo seria
    # un error garantizado en cada pasada.
    if os.environ.get("TABLERO_NUBE") == "1":
        return fallos
    try:
        import publicar_cines
        publicar_cines.main()
    except Exception as e:
        print("Cines: no se pudo publicar la web ({}: {}).".format(type(e).__name__, e))

    return fallos


if __name__ == "__main__":
    # Salir != 0 cuando una ciudad no quedo ok: es lo unico que el cron mira
    # para marcar la corrida como fallida. Ver la nota de refrescar_cineplanet.py.
    sys.exit(1 if main() else 0)

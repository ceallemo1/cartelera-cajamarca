# -*- coding: utf-8 -*-
"""
Cliente minimo de TMDb (The Movie Database) para el bloque de cines.

Por que TMDb: API gratuita (uso personal, sin tarjeta) que da calificacion de
criticos/usuarios (vote_average 0-10 + vote_count) y reseñas reales por pelicula.
Se empareja por TITULO (busqueda), asi que la mayoria matchea; los titulos locales
muy divergentes (ej. "Scary Movie Terrorificamente Incorrecta") pueden no encontrar
coincidencia: en ese caso se devuelve None y NO se inventa nada.

stdlib puro (urllib): el bloque de cines se mantiene sin dependencias.
La key sale de D:/Tools/secrets/tablero.env (TMDB_API_KEY), fuera de OneDrive.

Uso:
    from tmdb import enriquecer_tmdb
    enriquecer_tmdb(peli_dict)   # agrega peli["tmdb"] = {...} o no toca nada si no matchea
"""
import json
import os
import re
import urllib.parse
import urllib.request

ENV_PATH = os.environ.get("TABLERO_ENV", r"D:/Tools/secrets/tablero.env")


def _cargar_env(path=ENV_PATH):
    # El archivo manda cuando existe (la laptop); si no existe — la nube, donde el
    # secreto llega como variable de entorno — se cae a os.environ. Nunca al reves:
    # asi la maquina de Cesar sigue leyendo su unica fuente de verdad de claves.
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                k, _, v = linea.partition("=")
                cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        for k in ("TMDB_API_KEY", "TMDB_BASE_URL", "TMDB_IMG_BASE"):
            if os.environ.get(k):
                cfg[k] = os.environ[k]
    return cfg


_ENV = _cargar_env()
_KEY = _ENV.get("TMDB_API_KEY", "")
_BASE = _ENV.get("TMDB_BASE_URL", "https://api.themoviedb.org/3")
_IMG = _ENV.get("TMDB_IMG_BASE", "https://image.tmdb.org/t/p")


def disponible():
    """True si hay key configurada. El bloque salta TMDb si no la hay."""
    return bool(_KEY)


def _get(path, params):
    params = dict(params, api_key=_KEY)
    url = f"{_BASE.rstrip('/')}/{path.lstrip('/')}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _limpiar_titulo(t):
    """Recorta colas locales que TMDb no reconoce, para mejorar el match.
    Quita subtitulos tras ':' o '-' largos y palabras de marketing comunes en
    titulos peruanos. NO inventa: solo prueba variantes mas cortas del mismo titulo."""
    t = (t or "").strip()
    # variante 1: titulo completo. variante 2: antes de ':' o ' - '
    variantes = [t]
    for sep in (":", " - "):
        if sep in t:
            cabeza = t.split(sep)[0].strip()
            if len(cabeza) >= 3 and cabeza not in variantes:
                variantes.append(cabeza)
    return variantes


def _buscar(titulo):
    """Busca una pelicula por titulo (probando variantes). Devuelve el mejor
    resultado (dict TMDb) o None. Prioriza es-PE y cae a busqueda sin idioma."""
    for variante in _limpiar_titulo(titulo):
        for lang in ("es-PE", "es-ES", None):
            params = {"query": variante, "include_adult": "true"}
            if lang:
                params["language"] = lang
            try:
                data = _get("search/movie", params)
            except Exception:
                continue
            res = data.get("results") or []
            if res:
                # el primer resultado de TMDb ya viene ordenado por popularidad/relevancia
                return res[0]
    return None


def _detalle(movie_id):
    """Trae detalle + creditos + videos en UNA sola llamada (append_to_response).
    Devuelve el dict completo de TMDb o {} si falla."""
    for lang in ("es-PE", "es-ES", "en-US"):
        try:
            data = _get(f"movie/{movie_id}", {
                "language": lang,
                "append_to_response": "credits,videos",
            })
            if data and data.get("id"):
                return data
        except Exception:
            continue
    return {}


def _director(detalle):
    """Nombre(s) del/los director(es) desde credits.crew. '' si no hay."""
    crew = ((detalle.get("credits") or {}).get("crew")) or []
    dirs = [c.get("name") for c in crew if c.get("job") == "Director" and c.get("name")]
    return ", ".join(dict.fromkeys(dirs))  # sin duplicados, orden estable


def _reparto(detalle, limite=6):
    """Primeros `limite` actores principales desde credits.cast. [] si no hay."""
    cast = ((detalle.get("credits") or {}).get("cast")) or []
    cast = sorted(cast, key=lambda c: c.get("order", 999))
    out = []
    for c in cast[:limite]:
        nom = c.get("name")
        if not nom:
            continue
        out.append({"nombre": nom, "personaje": (c.get("character") or "").strip()})
    return out


def _generos(detalle):
    """Lista de nombres de género desde detalle.genres. [] si no hay."""
    return [g.get("name") for g in (detalle.get("genres") or []) if g.get("name")]


_PAIS_ES = {
    "US": "Estados Unidos", "GB": "Reino Unido", "ES": "España", "MX": "México",
    "FR": "Francia", "DE": "Alemania", "IT": "Italia", "JP": "Japón", "KR": "Corea del Sur",
    "CN": "China", "IN": "India", "AR": "Argentina", "BR": "Brasil", "CA": "Canadá",
    "AU": "Australia", "ID": "Indonesia", "PE": "Perú", "CO": "Colombia", "CL": "Chile",
}
_IDIOMA_ES = {
    "en": "Inglés", "es": "Español", "fr": "Francés", "de": "Alemán", "it": "Italiano",
    "ja": "Japonés", "ko": "Coreano", "zh": "Chino", "hi": "Hindi", "pt": "Portugués",
    "id": "Indonesio", "ru": "Ruso",
}


def _paises(detalle):
    """Países de producción en español. [] si no hay."""
    out = []
    for c in (detalle.get("production_countries") or []):
        code = c.get("iso_3166_1")
        out.append(_PAIS_ES.get(code, c.get("name") or code))
    return [x for x in out if x]


def _fmt_fecha(iso):
    """'2026-05-13' -> '13 may 2026'. Devuelve '' si no parsea."""
    meses = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    try:
        a, m, d = (iso or "").split("-")
        return f"{int(d)} {meses[int(m)]} {a}"
    except Exception:
        return ""


def _extra(detalle, m):
    """Campos adicionales para enriquecer la ficha: sinopsis (respaldo),
    estreno, duración exacta, país, idioma y título original. Solo lo que TMDb
    devuelve; nunca inventa."""
    runtime = detalle.get("runtime")
    return {
        "sinopsis_tmdb": (detalle.get("overview") or m.get("overview") or "").strip(),
        "estreno": _fmt_fecha(detalle.get("release_date") or m.get("release_date") or ""),
        "duracion_min": runtime if isinstance(runtime, int) and runtime else None,
        "paises": _paises(detalle),
        "idioma": _IDIOMA_ES.get(detalle.get("original_language") or m.get("original_language"),
                                 (detalle.get("original_language") or "").upper()),
        "titulo_original": (detalle.get("original_title") or m.get("original_title") or "").strip(),
    }


def _trailer(detalle):
    """Mejor tráiler de YouTube: devuelve {url, embed_id} o {}.
    Prioriza type=Trailer oficial en español, luego cualquier Trailer, luego Teaser.
    embed_id permite incrustar el reproductor; nunca inventa si no hay video."""
    vids = ((detalle.get("videos") or {}).get("results")) or []
    yt = [v for v in vids if (v.get("site") == "YouTube" and v.get("key"))]
    if not yt:
        return {}

    def rank(v):
        tipo = (v.get("type") or "").lower()
        t = 0 if tipo == "trailer" else (1 if tipo == "teaser" else 2)
        oficial = 0 if v.get("official") else 1
        es = 0 if (v.get("iso_639_1") in ("es",)) else 1
        return (t, oficial, es)

    yt.sort(key=rank)
    best = yt[0]
    key = best["key"]
    return {"url": f"https://www.youtube.com/watch?v={key}", "embed_id": key}


def enriquecer_tmdb(peli):
    """Agrega peli['tmdb'] = {voto, votos, tmdb_id, url_tmdb, poster_tmdb,
    director, reparto[], generos[], trailer{}} si matchea por titulo.
    Tambien rellena peli['director']/['actores']/['trailer_url'] si venian vacios
    (cruce de fuentes: Cineplanet no trae estos campos; TMDb si).
    Si no matchea o no hay key, deja la peli intacta. Nunca inventa."""
    if not _KEY:
        return peli
    m = _buscar(peli.get("titulo", ""))
    if not m:
        return peli
    mid = m.get("id")
    voto = m.get("vote_average")
    votos = m.get("vote_count") or 0
    det = _detalle(mid) if mid else {}
    director = _director(det)
    reparto = _reparto(det)
    generos = _generos(det)
    trailer = _trailer(det)
    extra = _extra(det, m)
    imdb_id = (det.get("imdb_id") or "").strip()
    tmdb = {
        "tmdb_id": mid,
        "titulo_tmdb": m.get("title") or m.get("original_title") or "",
        "voto": round(voto, 1) if isinstance(voto, (int, float)) and voto else None,
        "votos": votos,
        "url_tmdb": f"https://www.themoviedb.org/movie/{mid}" if mid else "",
        "imdb_id": imdb_id,
        "url_imdb": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "",
        "poster_tmdb": (_IMG.rstrip("/") + "/w500" + m["poster_path"]) if m.get("poster_path") else "",
        "director": director,
        "reparto": reparto,
        "generos": generos,
        "trailer": trailer,           # {url, embed_id} o {}
        "sinopsis_tmdb": extra["sinopsis_tmdb"],
        "estreno": extra["estreno"],
        "duracion_min": extra["duracion_min"],
        "paises": extra["paises"],
        "idioma": extra["idioma"],
        "titulo_original": extra["titulo_original"],
    }
    peli["tmdb"] = tmdb
    # Cruce de fuentes: rellenar campos vacios de la peli con lo de TMDb.
    if not (peli.get("director") or "").strip() and director:
        peli["director"] = director
    if not (peli.get("actores") or "").strip() and reparto:
        peli["actores"] = ", ".join(r["nombre"] for r in reparto)
    if not (peli.get("trailer_url") or "").strip() and trailer.get("url"):
        peli["trailer_url"] = trailer["url"]
    if not (peli.get("sinopsis") or "").strip() and extra["sinopsis_tmdb"]:
        peli["sinopsis"] = extra["sinopsis_tmdb"]
    return peli

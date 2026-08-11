# -*- coding: utf-8 -*-
"""
Cartelera de Cinerama, leida de su propia web.

POR QUE EXISTE
--------------
carteleracine.pe —la fuente del bloque de cines— LISTA la sala
(`cinerama-cajamarca-megaplaza-cajamarca-s471`) pero no publica sus funciones: su
ficha dice "no hay programacion disponible para este sala". Como los cines se deducen
del cruce con las fichas de pelicula, Cinerama desaparecia del tablero y este mostraba
2 cines en Cajamarca cuando hay 3. Cesar lo corrigio el 2026-08-05: *"hay 3 cines en
cajamarca"* y *"los 3 estan funcionando"*. Tenia razon — su web propia da 5 peliculas
y 16 funciones para el mismo dia en que carteleracine decia que no habia ninguna.

Es el mismo patron que ya se usa con Cineplanet: cuando la cadena publica su propia
cartelera, esa manda sobre el agregador.

DE DONDE SALE EL DATO
---------------------
El boton "VER CARTELERA" de https://www.cinerama.com.pe/cines llama por AJAX a
    /assets/ajax/obtener_peliculas.php?cine=CINERAMA_<SEDE>
y devuelve JSON: nombre, sinopsis, genero, duracion, censura, imagen y 'horarios'
[{hora, idioma, link}]. La direccion no se adivino: se leyo capturando las peticiones
de red al pulsar el boton, que es la unica forma de no equivocarse con una web asi.

POR QUE REINTENTA Y GUARDA COPIA
--------------------------------
El 2026-08-11 el cron de las 11:00 recibio un HTTP 500 de esta misma web y el tablero
publico "Cinerama: hoy no publico horarios" — falso: minutos despues el endpoint
devolvia 3 peliculas y 19 funciones del mismo dia. Cesar lo corrigio: *"todos los dias
tienen funciones en cajamarca"*. Un servidor caido 30 segundos no es una sala cerrada,
y la diferencia importa porque quien lee la pagina decide si sale de casa.

Por eso hay dos redes debajo:
  1. Se reintenta unas cuantas veces antes de rendirse (los 5xx de esta web son cortos).
  2. Lo ultimo que respondio bien se guarda en un side-cache. Si al final no responde,
     se reusa ESE dato siempre que sea del mismo dia — nunca de otro.
Y si no hay ninguna de las dos, el error viaja marcado como transitorio para que quien
pinte la pagina diga "no se pudo consultar" y no "no hay funciones".

LO QUE NO HACE
--------------
No inventa la sede. Si 'obtener_peliculas.php' responde BIEN y no trae funciones para
la sede pedida, se dice que no hay funciones y ya: es preferible una sala vacia y
honesta a una sala con horarios de otro sitio.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.cinerama.com.pe"
AJAX = BASE + "/assets/ajax/obtener_peliculas.php?cine={sede}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Sede por ciudad. La clave es el slug de ciudad que usa bloque_cines.
SEDES = {
    "cajamarca": {
        "sede": "CINERAMA_CAJAMARCA",
        "cine": "Cinerama Cajamarca MegaPlaza",
        "cine_slug": "cinerama-cajamarca-megaplaza",
        "direccion": "Jr. Sor Manuela Gil 151, CC MegaPlaza Cajamarca",
        "url": BASE + "/cines",
    },
}


# Ver la nota de bloque_cines.py: TABLERO_CACHE permite correr esto en la nube,
# donde no hay unidad D:. Sin la variable, todo queda como estaba.
CACHE_DIR = os.environ.get("TABLERO_CACHE", "D:/Tools/Tablero_cache")
CACHE = CACHE_DIR + "/cinerama_{ciudad}.json"

# Cuantas veces se reintenta y cuanto se espera entre intentos. Tres intentos con
# esperas cortas cubren el 500 que se vio (duro menos de un minuto) sin que el cron
# se quede colgado: el peor caso son ~100 s, y el bloque de cines corre en background.
INTENTOS = 3
ESPERAS = (3, 12)


def _transitorio(e):
    """¿Es un fallo del que vale la pena reintentar, o una respuesta definitiva?

    Un 500/502/503 o un timeout es la web teniendo un mal momento. Un 404 es una
    respuesta: reintentarlo solo gasta tiempo y retrasa el resto del tablero.
    """
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code == 429
    return isinstance(e, (urllib.error.URLError, TimeoutError, OSError))


def _fetch(url):
    """Pide el JSON reintentando los fallos pasajeros. Lanza el ultimo error si no hay caso."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",   # sin esto el PHP responde la portada
        "Referer": BASE + "/cines",
    })
    ultimo = None
    for i in range(INTENTOS):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            ultimo = e
            if not _transitorio(e) or i == INTENTOS - 1:
                raise
            time.sleep(ESPERAS[min(i, len(ESPERAS) - 1)])
    raise ultimo


def _guardar_cache(ciudad_slug, fecha, cine):
    """Deja la ultima respuesta buena en disco, con el dia al que corresponde."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE.format(ciudad=ciudad_slug), "w", encoding="utf-8") as f:
            json.dump({"fecha": fecha, "cine": cine}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass   # no poder guardar la copia no puede tumbar la lectura que si funciono


def _leer_cache(ciudad_slug, fecha):
    """Devuelve la copia guardada SOLO si es del mismo dia que se esta pidiendo.

    La restriccion no es un detalle: los horarios de ayer publicados como los de hoy
    mandarian a alguien al cine a una funcion que no existe. Sin fecha que comparar,
    tampoco se usa.
    """
    if not fecha:
        return None
    try:
        with open(CACHE.format(ciudad=ciudad_slug), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("fecha") == fecha and d.get("cine", {}).get("peliculas"):
            return d["cine"]
    except Exception:
        pass
    return None


def _a24h(h):
    """'03:00 PM' -> '15:00'. Deja intacto lo que ya venga en 24 h."""
    h = (h or "").strip().upper()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", h)
    if not m:
        return h.replace(" ", "")
    hh, mm, ap = int(m.group(1)), m.group(2), m.group(3)
    if ap == "PM" and hh != 12:
        hh += 12
    if ap == "AM" and hh == 12:
        hh = 0
    return "%02d:%s" % (hh, mm)


def _titulo(t):
    """'SPIDERMAN: UN NUEVO DIA (PREVENTA)' -> 'Spiderman: Un Nuevo Dia (Preventa)'.

    Cinerama publica TODO en mayusculas. Junto a los titulos de las otras dos fuentes
    —que vienen en capitalizacion normal— una fila en mayusculas se lee como un error
    de la pagina, y ademas rompe el emparejamiento por titulo entre fuentes.
    """
    t = re.sub(r"\s+", " ", (t or "").strip())
    if not t or t != t.upper():
        return t

    # Dos trampas de `str.capitalize()` que se vieron en la primera prueba:
    #   - "(PREVENTA)" -> "(preventa)", porque el primer caracter es un parentesis y
    #     no una letra. Hay que capitalizar la primera LETRA, no la primera posicion.
    #   - las palabras cortas hay que BAJARLAS ("LA Odisea" salia asi por devolver la
    #     palabra original en mayusculas en vez de su version en minusculas).
    menores = {"de", "del", "la", "el", "los", "las", "y", "a", "en", "un", "una"}

    def _cap(p):
        p = p.lower()
        for i, c in enumerate(p):
            if c.isalpha():
                return p[:i] + p[i].upper() + p[i + 1:]
        return p

    salida = []
    palabras = t.split()
    for i, p in enumerate(palabras):
        bajo = p.lower()
        # Tras dos puntos empieza frase nueva: "SPIDERMAN: UN NUEVO DIA" es
        # "Spiderman: Un Nuevo Dia", no "Spiderman: un Nuevo Dia".
        arranca = i == 0 or palabras[i - 1].endswith((":", "-", "."))
        salida.append(bajo if (not arranca and bajo.strip("()").isalpha() and bajo in menores)
                      else _cap(p))
    return " ".join(salida)


def cine_de(ciudad_slug, fecha=None):
    """Devuelve la ficha del cine de esa ciudad, en el formato de bloque_cines.

    Devuelve (cine_o_None, error_o_None). Nunca lanza: si Cinerama no responde, el
    bloque de cines tiene que seguir saliendo con las otras dos fuentes.

    El error empieza por 'NO_CONSULTADO:' cuando no se pudo hablar con la web. Quien
    pinte la pagina DEBE distinguirlo de "no hay funciones": lo primero es un problema
    nuestro, lo segundo un dato sobre el cine.
    """
    conf = SEDES.get(ciudad_slug)
    if not conf:
        return None, "sin sede de Cinerama configurada para " + str(ciudad_slug)
    try:
        crudo = _fetch(AJAX.format(sede=urllib.parse.quote(conf["sede"])))
        datos = json.loads(crudo)
    except Exception as e:
        guardado = _leer_cache(ciudad_slug, fecha)
        if guardado:
            return guardado, None
        return None, "NO_CONSULTADO: {}: {}".format(type(e).__name__, e)
    if not isinstance(datos, list):
        guardado = _leer_cache(ciudad_slug, fecha)
        if guardado:
            return guardado, None
        return None, "NO_CONSULTADO: respuesta inesperada de Cinerama"

    pelis = []
    dia_dato = None
    for p in datos:
        dia_dato = dia_dato or p.get("funcion")
        # 'funcion' es el dia al que corresponden esos horarios. Si viene y no es el
        # dia pedido, no se usa: pintar funciones de otro dia es peor que no pintarlas.
        if fecha and p.get("funcion") and p.get("funcion") != fecha:
            continue
        horas = [_a24h(h.get("hora")) for h in (p.get("horarios") or []) if h.get("hora")]
        horas = sorted(set(h for h in horas if h))
        if not horas:
            continue
        img = (p.get("imagen") or "").strip()
        pelis.append({
            "titulo": _titulo(p.get("nombre")),
            "poster": (BASE + "/_admin/assets/images/peliculas/" + urllib.parse.quote(img)) if img else "",
            "genero": _titulo(p.get("genero") or ""),
            "clasificacion": _titulo(p.get("censura") or ""),
            "duracion": (p.get("duracion") or "").replace(":00 min", "").strip(),
            "sinopsis": _titulo(p.get("sinopsis") or "")[:400],
            "formatos": [{"formato": "", "horarios": horas}],
            "horarios": horas,
        })

    if not pelis:
        # Aqui la web SI respondio: es un dato, no un fallo. Va sin 'NO_CONSULTADO'.
        return None, "Cinerama no publica funciones para esa fecha"
    pelis.sort(key=lambda x: x["titulo"])
    cine = {
        "cine": conf["cine"],
        "cine_slug": conf["cine_slug"],
        "direccion": conf["direccion"],
        "url": conf["url"],
        "fuente_cine": "Cinerama",
        "peliculas": pelis,
    }
    # Se guarda con el dia al que pertenecen los horarios. Si no se pidio fecha, el que
    # declara la propia web ('funcion'): un respaldo sin dia no se puede reusar.
    _guardar_cache(ciudad_slug, fecha or dia_dato, cine)
    return cine, None


if __name__ == "__main__":
    import sys
    c, err = cine_de(sys.argv[1] if len(sys.argv) > 1 else "cajamarca")
    if err:
        print("error:", err)
    else:
        print("%s — %d peliculas" % (c["cine"], len(c["peliculas"])))
        for p in c["peliculas"]:
            print("  %-34s %s" % (p["titulo"][:34], ", ".join(p["horarios"])))

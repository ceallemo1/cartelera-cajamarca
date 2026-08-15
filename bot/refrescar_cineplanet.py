# -*- coding: utf-8 -*-
"""
Refresca el cache lateral de CINEPLANET para el bloque de cines.

Por que existe (y por que esta SEPARADO de bloque_cines.py):
  Cineplanet expone 3 caches JSON same-origin sin API key:
    - cinemascache  -> lista de cines (ID, name, formattedCinemaName, lat/lon...)
    - sessioncache  -> sesiones {id:"<cineID>-<sessionId>", showtime, screenName,
                       formats, languages}. NO trae el titulo de la pelicula.
    - moviescache   -> peliculas; CADA una lista cinemas[].dates[].sessions[] (ids).
                       Es el UNICO lugar que une sesion -> pelicula.
  cinemascache + sessioncache se leen desde Python plano (HTTP 200), pero
  moviescache esta protegido por un WAF: devuelve 403 a Python/curl y solo 200
  desde un navegador real con sesion "asentada". Por eso este refresco usa
  Playwright (Chromium headless) y bloque_cines.py se mantiene stdlib puro:
  el bloque solo LEE el JSON que este script deja en cache.

Patron anti-WAF verificado (2026-06-30): navegar la SPA /peliculas con
wait_until=networkidle, enmascarar navigator.webdriver, UA real y una pausa
breve para que el WAF asiente la sesion -> moviescache responde 200.

Salida: D:/Tools/Tablero_cache/cineplanet_<ciudad>.json
Forma (misma que produce carteleracine para encajar en el bloque):
  {
    "fuente_cine": "Cineplanet",
    "actualizado": "YYYY-MM-DD HH:MM",
    "estado": "ok" | "error",
    "fecha_objetivo": "YYYY-MM-DD",
    "cines": [ { "cine","cine_slug","direccion","url",
                 "peliculas":[ {"titulo","poster","genero","clasificacion","duracion",
                                "sinopsis","formatos":[{formato,horarios:[]}],"horarios":[]} ] } ]
  }

Requiere el venv con Playwright: D:/Tools/venvs/tablero_playwright
Lo corre un cron aparte (ver _registrar_tablero_jobs.py: tablero-cineplanet).
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

PERU = timezone(timedelta(hours=-5))
# Ver la nota de bloque_cines.py: TABLERO_CACHE permite correr esto en la nube,
# donde no hay unidad D:. Sin la variable, todo queda como estaba.
CACHE_DIR = os.environ.get("TABLERO_CACHE", "D:/Tools/Tablero_cache")

# Ciudad -> como reconocer sus cines en cinemascache (campo "city" de Cineplanet).
# CAJAMARCA Y NADA MAS (decision de Cesar, 2026-08-03): el bloque de cines cubre
# solo la ciudad donde vive. Lima se retiro entera del bloque, no solo de aqui.
# Si algun dia vuelve: el campo "city" de cinemascache ya viene agrupado por
# DEPARTAMENTO (los 27 locales limenos traen city:"Lima"), asi que basta con
# agregar la linea -- no hace falta lista de IDs ni heuristica de distrito.
CIUDADES = {
    "cajamarca": {"city": "Cajamarca", "etiqueta": "Cajamarca"},
}

# JS que corre DENTRO del navegador: baja los 3 caches y arma el indice por cine
# para la ciudad pedida, resolviendo sesion -> pelicula via moviescache.
JS_RESOLVER = r"""
async (cityName) => {
  const B = 'https://www.cineplanet.com.pe/api/v1-web/cache/';

  /* El WAF no cae del todo: cae A RATOS. Cuando decide que la sesion no le
     convence devuelve su pagina de desafio (HTML) con status 200, y r.json()
     revienta con "Unexpected token '<'". Eso es lo que dejo Cineplanet fuera
     del tablero durante horas el 2026-08-03 sin que se notara.
     Dos cambios: se reintenta con una pausa (la sesion suele asentarse sola en
     el segundo intento) y el error dice QUE endpoint fallo — antes el
     Promise.all los juntaba y el mensaje no permitia ni empezar a mirar. */
  async function bajar(nombre) {
    let ultimo = '';
    for (let i = 0; i < 3; i++) {
      if (i) await new Promise(r => setTimeout(r, 1500 * i));
      try {
        const r = await fetch(B + nombre);
        const t = await r.text();
        if (t.trim().startsWith('<')) {         // pagina del WAF, no datos
          ultimo = `${nombre}: HTML en vez de JSON (status ${r.status})`;
          continue;
        }
        /* Desde el 2026-08-15 el WAF NO devuelve HTML: devuelve JSON valido
           {"status":403,"error":"Forbidden"} con codigo 403. Sin esta guarda el
           JSON.parse funcionaba, el objeto no traia 'cinemas', y el resultado
           salia como estado="vacio" con error=None: un fallo total disfrazado de
           cartelera sin funciones. El status manda sobre la forma del cuerpo. */
        if (!r.ok) {
          ultimo = `${nombre}: HTTP ${r.status} (WAF) ${t.slice(0, 120)}`;
          continue;
        }
        return JSON.parse(t);
      } catch (e) {
        ultimo = `${nombre}: ${e && e.message ? e.message : e}`;
      }
    }
    throw new Error(ultimo || `${nombre}: sin respuesta util`);
  }

  // En serie, no en paralelo: tres peticiones simultaneas es justo el patron
  // que al WAF le parece un bot.
  const cm = await bajar('cinemascache');
  const sm = await bajar('sessioncache');
  const mm = await bajar('moviescache');

  // Cines de la ciudad pedida (campo city). ID en MAYUSCULAS.
  const cinesCiudad = (cm.cinemas || []).filter(
    c => (c.city || '').toLowerCase() === cityName.toLowerCase()
  );
  const cineById = {};
  cinesCiudad.forEach(c => { cineById[c.ID] = c; });
  const idsCiudad = new Set(cinesCiudad.map(c => c.ID));

  // Indice de sesiones por id ("<cineID>-<sessionId>")
  const sessById = {};
  (sm.sessions || []).forEach(s => { sessById[s.id] = s; });

  // Fecha objetivo = HOY en hora Peru (las sesiones traen offset -05:00).
  const ahora = new Date();
  // YYYY-MM-DD en zona -05:00
  const peru = new Date(ahora.getTime() - (5 * 60 + ahora.getTimezoneOffset()) * 60000);
  const hoy = peru.toISOString().slice(0, 10);

  // Acumulador por cine -> peliculas
  const acc = {};  // cineID -> { cine, peliculas: { titulo -> {meta, formatos{}, horarios[]} } }
  idsCiudad.forEach(id => {
    const c = cineById[id];
    acc[id] = {
      cine: c.name,
      cine_slug: c.formattedCinemaName || '',
      // address = calle; secondAddress en el origen llega con la ciudad repetida
      // ("Cajamarca Cajamarca Cajamarca"). Unir y colapsar palabras consecutivas
      // duplicadas (sin inventar: solo se elimina la repeticion del dato origen).
      direccion: (function () {
        const partes = [c.address, c.secondAddress].filter(Boolean)
          .map(x => x.trim()).join(', ');
        const palabras = partes.split(/\s+/);
        const out = [];
        for (const w of palabras) {
          if (!out.length || out[out.length - 1].toLowerCase() !== w.toLowerCase()) {
            out.push(w);
          }
        }
        return out.join(' ').replace(/,\s*,/g, ',').replace(/\s+,/g, ',').trim();
      })(),
      url: 'https://www.cineplanet.com.pe/cinemas/' + (c.formattedCinemaName || ''),
      // Pagina de cartelera del cine: ahi sale el precio real del dia y se compra.
      comprar: 'https://www.cineplanet.com.pe/cinemas/' + (c.formattedCinemaName || ''),
      peliculas: {},
    };
  });

  for (const mov of (mm.movies || [])) {
    for (const cin of (mov.cinemas || [])) {
      if (!idsCiudad.has(cin.cinemaId)) continue;
      const bucket = acc[cin.cinemaId];
      for (const d of (cin.dates || [])) {
        // d.date viene como "2026-06-30T00:00:00"; comparar por prefijo de fecha.
        if (!(d.date || '').startsWith(hoy)) continue;
        for (const sid of (d.sessions || [])) {
          const ss = sessById[sid];
          if (!ss) continue;
          const hora = (ss.showtime || '').slice(11, 16);  // "HH:MM"
          if (!hora) continue;
          const fmt = [].concat(ss.formats || [], ss.languages || []).join(' ');
          if (!bucket.peliculas[mov.title]) {
            bucket.peliculas[mov.title] = {
              titulo: mov.title,
              poster: mov.posterUrl || mov.thumbnailUrl || '',
              genero: mov.genre || '',
              clasificacion: mov.ratingDescription || mov.restricted || '',
              duracion: mov.runTime ? (mov.runTime + ' min') : '',
              sinopsis: (mov.synopsis || '').replace(/<[^>]+>/g, '').trim().slice(0, 400),
              trailer_url: mov.trailer || '',
              _fmt: {},      // formato -> Set de horas
              _horas: {},    // union -> Set
            };
          }
          const P = bucket.peliculas[mov.title];
          (P._fmt[fmt] = P._fmt[fmt] || {})[hora] = 1;
          P._horas[hora] = 1;
        }
      }
    }
  }

  // Aplanar a la forma final (arrays ordenados), descartar cines sin funciones hoy.
  const cines = [];
  for (const id of Object.keys(acc)) {
    const c = acc[id];
    const pelis = [];
    for (const t of Object.keys(c.peliculas)) {
      const P = c.peliculas[t];
      const formatos = Object.keys(P._fmt).map(f => ({
        formato: f,
        horarios: Object.keys(P._fmt[f]).sort(),
      })).filter(x => x.horarios.length);
      const horarios = Object.keys(P._horas).sort();
      if (!horarios.length) continue;
      delete P._fmt; delete P._horas;
      pelis.push(Object.assign(P, { formatos, horarios }));
    }
    if (!pelis.length) continue;
    pelis.sort((a, b) => a.titulo.localeCompare(b.titulo, 'es'));
    cines.push({
      cine: c.cine, cine_slug: c.cine_slug, direccion: c.direccion,
      url: c.url, comprar: c.comprar, peliculas: pelis,
    });
  }
  cines.sort((a, b) => a.cine.localeCompare(b.cine, 'es'));
  return { fecha_objetivo: hoy, cines };
}
"""


def _refrescar_ciudad(pg, ciudad_slug, conf, ahora):
    res = {
        "fuente_cine": "Cineplanet",
        "actualizado": ahora.strftime("%Y-%m-%d %H:%M"),
        "estado": "ok",
        "ciudad": ciudad_slug,
        "fecha_objetivo": "",
        "cines": [],
    }
    # Segundo intento con la sesion RECIEN asentada: si el WAF ya rechazo los
    # reintentos de dentro del navegador, lo que suele faltar no es esperar mas
    # sino volver a pasar por la SPA. Cuesta unos segundos y es la diferencia
    # entre tener Cineplanet o no tenerlo en toda la jornada.
    for intento in (1, 2):
        try:
            data = pg.evaluate(JS_RESOLVER, conf["city"])
            res["fecha_objetivo"] = data.get("fecha_objetivo", "")
            res["cines"] = data.get("cines", [])
            res["estado"] = "ok" if res["cines"] else "vacio"
            res.pop("error", None)
            return res
        except Exception as e:
            res["estado"] = "error"
            res["error"] = f"{type(e).__name__}: {e}"
            if intento == 1:
                try:
                    pg.goto("https://www.cineplanet.com.pe/peliculas",
                            wait_until="networkidle", timeout=45000)
                    pg.wait_for_timeout(4000)
                except Exception:
                    break
    return res


def main():
    from playwright.sync_api import sync_playwright

    ahora = datetime.now(PERU)
    pedidas = [a for a in sys.argv[1:] if a in CIUDADES] or list(CIUDADES)
    os.makedirs(CACHE_DIR, exist_ok=True)

    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = b.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="es-PE", viewport={"width": 1280, "height": 860},
        )
        pg = ctx.new_page()
        pg.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        # Asienta la sesion (pasa el WAF) navegando la SPA real.
        pg.goto("https://www.cineplanet.com.pe/peliculas",
                wait_until="networkidle", timeout=45000)
        pg.wait_for_timeout(2500)

        fallos = 0
        for ciudad_slug in pedidas:
            res = _refrescar_ciudad(pg, ciudad_slug, CIUDADES[ciudad_slug], ahora)
            if res["estado"] != "ok":
                fallos += 1
            out = os.path.join(CACHE_DIR, f"cineplanet_{ciudad_slug}.json")
            # Una corrida fallida NO debe borrar datos buenos del mismo dia: hasta
            # hoy pisaba el cache con {"cines":[]} y el tablero perdia Cineplanet
            # hasta la pasada siguiente, aunque tuviera la cartelera correcta de
            # hace tres horas. Se conserva el bueno y se deja anotado el fallo.
            if res["estado"] != "ok":
                previo = None
                try:
                    with open(out, encoding="utf-8") as f:
                        previo = json.load(f)
                except Exception:
                    previo = None
                if (previo and previo.get("estado") == "ok"
                        and previo.get("fecha_objetivo") == ahora.strftime("%Y-%m-%d")):
                    previo["ultimo_fallo"] = {
                        "cuando": res["actualizado"],
                        "detalle": res.get("error", res["estado"]),
                    }
                    with open(out, "w", encoding="utf-8") as f:
                        json.dump(previo, f, ensure_ascii=False, indent=2)
                    print(f"Cineplanet {CIUDADES[ciudad_slug]['etiqueta']}: fallo "
                          f"({res.get('error', res['estado'])}); se conserva el cache "
                          f"bueno de {previo.get('actualizado')}.")
                    continue
            with open(out, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
            n = len(res["cines"])
            npel = sum(len(c["peliculas"]) for c in res["cines"])
            if res["estado"] == "ok":
                print(f"Cineplanet {CIUDADES[ciudad_slug]['etiqueta']}: "
                      f"{n} cine(s), {npel} pelicula-funciones ({res['fecha_objetivo']}).")
            else:
                print(f"Cineplanet {CIUDADES[ciudad_slug]['etiqueta']}: "
                      f"{res['estado']} {res.get('error','')}")
        b.close()
    return fallos


if __name__ == "__main__":
    # El codigo de salida es lo UNICO que mira el cron. Hasta hoy siempre salia
    # 0: la pasada de las 16:50 del 2026-08-03 escribio {"estado":"error",
    # "cines":[]} y OpenClaw la registro como "ok", asi que el fallo estuvo
    # horas a la vista de nadie. Un script que escribe su propio error y despues
    # informa exito no es un script que falla: es un script que miente.
    sys.exit(1 if main() else 0)

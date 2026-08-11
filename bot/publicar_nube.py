# -*- coding: utf-8 -*-
"""Decide si la cartelera recien scrapeada en la nube MERECE reemplazar a la publicada.

Por que existe. En la laptop el cache sobrevive entre corridas: si Cineplanet falla
una pasada, se conserva el archivo bueno de hace tres horas y la cartelera sale
completa igual. En la nube cada corrida arranca con el disco vacio, asi que una
pasada con Cineplanet caido produciria un JSON legitimo pero POBRE — y publicarlo
borraria la cartelera buena de la pasada anterior. La pagina pasaria de tres cines
con funciones a uno, sin que nadie hiciera nada mal.

Regla: el dato FRESCO manda sobre el viejo, salvo que sea peor el mismo dia.

  - estado != ok, o cero peliculas  -> no se publica nunca.
  - la publicada es de un dia ANTERIOR -> se publica (aunque traiga menos cines:
    una cartelera de hoy incompleta sigue siendo mejor que una de ayer completa,
    y la pagina rotula con claridad al cine que no se pudo consultar).
  - la publicada es de HOY y la nueva trae MENOS cines con funciones -> no se
    publica: es una regresion, no una actualizacion.

Salida: escribe el archivo destino solo si procede. Codigo 0 siempre que no haya
un fallo real — "no habia nada mejor que publicar" no es un error del workflow.
"""
import json
import os
import shutil
import sys


def _cines_con_funciones(d):
    return sum(1 for c in d.get("cines", [])
               if c.get("peliculas") and not c.get("sin_programacion"))


def main():
    nuevo_path, destino = sys.argv[1], sys.argv[2]
    with open(nuevo_path, encoding="utf-8") as f:
        nuevo = json.load(f)

    if nuevo.get("estado") != "ok" or not nuevo.get("peliculas"):
        print(f"NO se publica: estado={nuevo.get('estado')}, "
              f"{len(nuevo.get('peliculas', []))} peliculas.")
        return 0

    viejo = None
    if os.path.exists(destino):
        try:
            with open(destino, encoding="utf-8") as f:
                viejo = json.load(f)
        except Exception:
            viejo = None  # publicado ilegible: cualquier cosa sana lo mejora

    if viejo and viejo.get("fecha_objetivo") == nuevo.get("fecha_objetivo"):
        a, b = _cines_con_funciones(nuevo), _cines_con_funciones(viejo)
        if a < b:
            print(f"NO se publica: mismo dia y menos cines con funciones "
                  f"({a} < {b}). Se conserva la cartelera publicada.")
            return 0

    shutil.copyfile(nuevo_path, destino)
    print(f"Publicado: {len(nuevo['peliculas'])} peliculas, "
          f"{_cines_con_funciones(nuevo)} cine(s) con funciones, "
          f"dia {nuevo.get('fecha_objetivo')}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

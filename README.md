# Cartelera de cines — Cajamarca

Pagina estatica con la cartelera del dia de los cines de Cajamarca:
**https://ceallemo1.github.io/cartelera-cajamarca/**

Los datos salen de tres fuentes — carteleracine.pe (agregador), la web de Cineplanet
y la de Cinerama — y se refrescan **en la nube**, cinco veces al dia. No hace falta que
ninguna computadora este encendida.

## Como se actualiza

`.github/workflows/cartelera.yml` corre a las 07:00, 08:00, 11:00, 17:00 y 20:00 de Lima
(en el archivo van en UTC, que es Lima +5). Cada pasada:

1. levanta Chromium y lee Cineplanet, que publica su cartelera con JavaScript;
2. corre el scraper (`bot/bloque_cines.py`) y funde las tres fuentes;
3. **decide si publicar** (`bot/publicar_nube.py`) — ver abajo;
4. hace commit de `data/cines_cajamarca.json` si cambio.

Tambien se puede disparar a mano desde la pestana **Actions → Refrescar cartelera →
Run workflow**.

## Por que no se publica siempre

En la nube cada pasada arranca con el disco vacio. Si Cineplanet no responde una vez,
el scraper devuelve un JSON legitimo pero **pobre**, y publicarlo borraria la cartelera
buena de tres horas antes: la pagina pasaria de tres cines a uno sin que nadie hiciera
nada mal. Por eso `publicar_nube.py` solo reemplaza si el dato es de un dia nuevo, o si
siendo del mismo dia no trae menos cines con funciones.

Cuando una fuente no se pudo consultar, la pagina lo dice con esas palabras. Es distinto
de "este cine no tiene funciones hoy", y se distinguen a proposito.

## Que hay aqui

| Ruta | Que es |
|---|---|
| `index.html` | la pagina; lee el JSON y avisa si el dato no es de hoy |
| `data/cines_cajamarca.json` | la cartelera publicada (lo unico que se publica) |
| `bot/` | copia de los scripts del scraper, la que corre la nube |
| `.github/workflows/cartelera.yml` | el programa de las cinco pasadas |

Los scripts de `bot/` son **copia**: el original vive en la laptop y se sincroniza solo
(`agentes/sincronizar_bot.py`). No editarlos aqui, se pisan en la siguiente publicacion.

## Sinopsis y resenas

Son opcionales y dependen de claves guardadas como *secrets* del repo
(`TMDB_API_KEY`, `GEMINI_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`). Sin ellas la
cartelera sale igual de completa en lo que importa — cines, formatos, horarios y
posters — pero sin resumen de cada pelicula.

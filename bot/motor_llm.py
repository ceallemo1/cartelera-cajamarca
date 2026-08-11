# -*- coding: utf-8 -*-
"""
Motor LLM con cascada perpetua para el Tablero Personal.
Orden: CEREBRAS (base) -> GEMINI (calidad) -> GROQ (respaldo). NIM = reserva premium (explicita).
Todo gratis, sin tarjeta. Verificado en vivo 2026-06-28.

Uso:
    from motor_llm import resumir, llamar
    texto = resumir("Resume estas noticias:\n...", max_tokens=300)
    texto = llamar(prompt, motor="nim")   # forzar reserva premium

Las keys salen de D:/Tools/secrets/tablero.env (fuera de OneDrive).
"""
import json
import os
import urllib.request
import urllib.error
from datetime import date

ENV_PATH = os.environ.get("TABLERO_ENV", r"D:/Tools/secrets/tablero.env")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"  # evita Cloudflare 1010 en Groq

# Buzon de fallos DUROS para el vigia (scripts/vigia_modelos.py en Hermes).
# Por que existe: la cascada es silenciosa por diseno — si un motor muere, se salta
# al siguiente y todo "funciona". Asi estuvo NIM muerto 9 dias (410 Gone, 2026-07-23
# al 2026-08-01) sin que nadie lo notara. Un 404/410 NO es un fallo transitorio: el
# modelo dejo de existir y hay que cambiar el *_MODEL. Se deja anotado aqui y el
# vigia lo reporta a Cesar. Coste cero: no agrega ni una peticion de red.
ALERTAS_PATH = os.environ.get(
    "TABLERO_CACHE", r"D:/Tools/Tablero_cache") + "/cascada_alertas.json"
HTTP_DUROS = {400, 401, 403, 404, 410}


def _cargar_env(path=ENV_PATH):
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
        # No hay archivo de secretos: es la nube. Las claves llegan por entorno
        # (secrets del repo). En la laptop este ramal no se toca nunca.
        for k, v in os.environ.items():
            if v and k.endswith(("_API_KEY", "_MODEL", "_BASE_URL")):
                cfg[k] = v
    return cfg


_ENV = _cargar_env()


def _registrar_alerta(motor, codigo):
    """Anota un fallo duro para que el vigia lo reporte. NUNCA rompe la cascada:
    si falla la escritura se ignora — avisar es secundario frente a responder."""
    try:
        alertas = []
        if os.path.exists(ALERTAS_PATH):
            with open(ALERTAS_PATH, encoding="utf-8") as f:
                alertas = json.load(f).get("alertas", [])
        item = {"fecha": date.today().isoformat(), "motor": motor,
                "modelo": _ENV.get(motor.upper() + "_MODEL", "?"), "http": codigo}
        # dedup: un motor muerto falla en cada corrida; basta una entrada por dia
        if any(a.get("motor") == item["motor"] and a.get("modelo") == item["modelo"]
               and a.get("http") == codigo and a.get("fecha") == item["fecha"]
               for a in alertas):
            return
        alertas = (alertas + [item])[-20:]
        os.makedirs(os.path.dirname(ALERTAS_PATH), exist_ok=True)
        tmp = ALERTAS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"alertas": alertas}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ALERTAS_PATH)
    except Exception:
        pass


def _post(url, payload, headers, timeout=40):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _openai_compat(base, model, key, prompt, max_tokens, timeout, extra=None):
    """Cerebras, Groq y NIM hablan el dialecto OpenAI."""
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0.3}
    if extra:
        payload.update(extra)
    out = _post(
        base.rstrip("/") + "/chat/completions",
        payload,
        {"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": UA},
        timeout,
    )
    msg = out["choices"][0]["message"]
    # gpt-oss-120b separa razonamiento (reasoning) de la respuesta final (content)
    return (msg.get("content") or "").strip()


def _gemini(base, model, key, prompt, max_tokens, timeout):
    out = _post(
        f"{base.rstrip('/')}/models/{model}:generateContent?key={key}",
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3}},
        {"Content-Type": "application/json", "User-Agent": UA},
        timeout,
    )
    return out["candidates"][0]["content"]["parts"][0]["text"].strip()


# motor -> (funcion, claves env requeridas)
_MOTORES = {
    "cerebras": (lambda p, mt, to: _openai_compat(
        _ENV["CEREBRAS_BASE_URL"], _ENV["CEREBRAS_MODEL"], _ENV["CEREBRAS_API_KEY"], p, mt, to,
        extra={"reasoning_effort": "low"})),  # evita que el razonamiento agote tokens y deje content vacio
    "gemini": (lambda p, mt, to: _gemini(
        _ENV["GEMINI_BASE_URL"], _ENV["GEMINI_MODEL"], _ENV["GEMINI_API_KEY"], p, mt, to)),
    "groq": (lambda p, mt, to: _openai_compat(
        _ENV["GROQ_BASE_URL"], _ENV["GROQ_MODEL"], _ENV["GROQ_API_KEY"], p, mt, to,
        extra={"reasoning_effort": "low"})),  # idem cerebras: GROQ_MODEL paso a gpt-oss-120b
    # (2026-08-02) y sin esto devolvia VACIO 3/3 con max_tokens=120 (bloque_updates): el
    # razonamiento se comia el presupuesto. Con "low" baja de 261 a 27 chars. OJO: el valor
    # "none" NO existe en Groq (HTTP 400), solo low/medium/high.
    "nim": (lambda p, mt, to: _openai_compat(
        _ENV["NIM_BASE_URL"], _ENV["NIM_MODEL"], _ENV["NIM_API_KEY"], p, mt, to)),
}

CASCADA = ["cerebras", "gemini", "groq"]  # nim NO entra en cascada: reserva premium explicita


def llamar(prompt, motor=None, max_tokens=400, timeout=40):
    """Llama a un motor concreto (motor=...) o recorre la cascada hasta que uno responda.
    Devuelve (texto, motor_usado). Lanza RuntimeError si todos fallan."""
    orden = [motor] if motor else CASCADA
    errores = []
    for m in orden:
        if m not in _MOTORES:
            errores.append(f"{m}: motor desconocido")
            continue
        try:
            txt = _MOTORES[m](prompt, max_tokens, timeout)
            if txt:
                return txt, m
            errores.append(f"{m}: respuesta vacia")
        except urllib.error.HTTPError as e:
            errores.append(f"{m}: HTTP {e.code}")
            if e.code in HTTP_DUROS:  # modelo muerto/inexistente: que no pase callado
                _registrar_alerta(m, e.code)
        except Exception as e:
            errores.append(f"{m}: {type(e).__name__} {e}")
    raise RuntimeError("Todos los motores fallaron -> " + " | ".join(errores))


def resumir(prompt, max_tokens=400, timeout=40):
    """Atajo: cascada normal, devuelve solo el texto."""
    txt, _ = llamar(prompt, max_tokens=max_tokens, timeout=timeout)
    return txt


if __name__ == "__main__":
    import sys
    io_enc = sys.stdout
    p = "Resume en una frase en espanol: hoy hay cine en Cajamarca a las 16:00 y 19:00."
    t, m = llamar(p, max_tokens=60)
    print(f"[motor={m}] {t}")

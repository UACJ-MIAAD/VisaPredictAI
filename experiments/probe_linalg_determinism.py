"""M74-F · deuda 6 — sonda: ¿el `LinAlgError` de ARIMA/SARIMA depende de los hilos de BLAS?

Dos campañas sobre el MISMO panel excluyeron conjuntos distintos de combinaciones ARIMA/SARIMA por
``LinAlgError: LU decomposition error`` (registro: ``docs/m74f_linalg_exclusiones.json``). La
hipótesis es el no-determinismo de BLAS multihilo (numpy usa Accelerate en macOS arm64, que
``threadpoolctl`` no controla: los hilos sólo se piden con ``VECLIB_MAXIMUM_THREADS`` ANTES de que el
proceso cargue numpy). Esta sonda NO decide nada: mide.

Para cada combinación del registro (la unión de las dos campañas), cada configuración de hilos y
cada repetición lanza un **subproceso nuevo** que ejecuta exactamente el ajuste de la etapa 1
(`walkforward.run_forecasts`, el mismo que llama `walkforward.backtest`) y devuelve el desenlace
(``ok`` / clase de excepción) y el sha256 de los pronósticos. Resume por combinación × hilos:
cuántos ajustes fallan y cuántos pronósticos distintos produce.

Hilos **solicitados**, no **efectivos**: ``1`` fija las variables de ``THREAD_VARS`` en el entorno
del subproceso y ``default`` las retira; el campo ``threads`` del informe registra esa SOLICITUD.
La sonda no mide cuántos hilos usó de verdad Accelerate (no hay API que lo exponga y
``threadpoolctl`` no lo ve), así que ninguna lectura puede afirmar el número efectivo.

Lectura del resultado. ``deterministic`` significa «no se observó variabilidad en los K ensayos»
(un desenlace y, como mucho, un hash), no determinismo probado:

* sin variabilidad con 1 hilo solicitado y con variabilidad con los hilos por defecto ⇒ **evidencia
  compatible** con BLAS multihilo como causa, acotada a esta máquina, estas versiones y K;
* sin variabilidad observada en ninguna configuración ⇒ la sonda **no reprodujo** el fenómeno; K
  ensayos estables no excluyen el no-determinismo, y quedan abiertas otras causas (orden de carga,
  versión de librería, estado previo del proceso, carga de la máquina);
* variabilidad también con 1 hilo solicitado ⇒ la solicitud no basta para estabilizarlo, o el
  origen no es el número de hilos.

No entrena modelos de la campaña, no escribe en ``reports/``: el destino es obligatorio (``--out``).

Uso (desde la raíz, en ``ante``):
    ante/bin/python experiments/probe_linalg_determinism.py --dry-run
    ante/bin/python experiments/probe_linalg_determinism.py --out /ruta/fuera/de/reports/probe.json --repeats 5
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "m74f_linalg_exclusiones.json"
#: Variables con que se SOLICITAN los hilos de los BLAS conocidos (Accelerate lee VECLIB_MAXIMUM_THREADS).
THREAD_VARS = ("VECLIB_MAXIMUM_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
CONFIGS = ("1", "default")
#: Segundos medidos por ajuste en la etapa 1 de `rederiv_953cd00_20261006T073927` (mediana) + arranque.
SECS = {"sarima": 16.1, "arima": 5.9}
ARRANQUE = 6.0

_HIJO = r"""
import hashlib, json, sys, time, warnings
t0 = time.time()
model, country, category, table = sys.argv[1:5]
try:
    from vp_model import walkforward
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _ts, fc = walkforward.run_forecasts(model, country, category, table)
    v = fc.values().astype("float64").tobytes()
    out = {"status": "ok", "forecast_sha256": hashlib.sha256(v).hexdigest(), "n": int(len(fc))}
except Exception as e:
    out = {"status": type(e).__name__, "error": str(e)[:160], "forecast_sha256": None, "n": 0}
out["secs"] = round(time.time() - t0, 2)
print("PROBE_RESULT " + json.dumps(out))
"""


@dataclass(frozen=True)
class Combo:
    table: str
    country: str
    category: str
    model: str
    campaigns: tuple[str, ...]


def cargar_combos(registro: Path = REGISTRY) -> list[Combo]:
    """La unión de las exclusiones registradas, con las campañas que excluyeron cada una."""
    datos = json.loads(registro.read_text(encoding="utf-8"))
    if datos.get("schema") != "linalg-exclusions-registry/1":
        raise ValueError(f"registro con esquema {datos.get('schema')!r}")
    union: dict[tuple[str, str, str, str], set[str]] = {}
    for campana, por_tabla in datos["campanas"].items():
        for tabla, filas in por_tabla.items():
            for country, category, model in filas:
                union.setdefault((tabla, country, category, model), set()).add(campana)
    return [Combo(*k, campaigns=tuple(sorted(v))) for k, v in sorted(union.items())]


def entorno(config: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Entorno del subproceso: ``1`` fija un hilo en todos los BLAS; ``default`` los deja sin fijar."""
    env = dict(os.environ if base is None else base)
    for var in THREAD_VARS:
        env.pop(var, None)
    if config == "1":
        env.update(dict.fromkeys(THREAD_VARS, "1"))
    elif config != "default":
        raise ValueError(f"configuración de hilos desconocida: {config!r}")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def coste_estimado(combos: list[Combo], repeats: int, configs: tuple[str, ...] = CONFIGS) -> float:
    """Segundos estimados, secuencial (la sonda no paraleliza: competir por CPU contaminaría la medida)."""
    return sum((SECS[c.model] + ARRANQUE) * repeats * len(configs) for c in combos)


def ensayo(combo: Combo, config: str) -> dict:
    fin = subprocess.run(
        [sys.executable, "-c", _HIJO, combo.model, combo.country, combo.category, combo.table],
        cwd=ROOT,
        env=entorno(config),
        capture_output=True,
        text=True,
        check=False,
    )
    linea = next((ln for ln in fin.stdout.splitlines() if ln.startswith("PROBE_RESULT ")), None)
    if linea is None:
        return {"status": "sin_resultado", "returncode": fin.returncode, "stderr": fin.stderr[-300:]}
    return json.loads(linea.removeprefix("PROBE_RESULT "))


def resumir(filas: list[dict]) -> list[dict]:
    grupos: dict[tuple, list[dict]] = {}
    for f in filas:
        grupos.setdefault((f["table"], f["country"], f["category"], f["model"], f["threads"]), []).append(f)
    out = []
    for (tabla, pais, cat, modelo, hilos), fs in sorted(grupos.items()):
        estados = [f["status"] for f in fs]
        hashes = {f["forecast_sha256"] for f in fs if f["forecast_sha256"]}
        out.append(
            {
                "table": tabla,
                "country": pais,
                "category": cat,
                "model": modelo,
                "threads": hilos,
                "n": len(fs),
                "n_ok": estados.count("ok"),
                "n_fail": len(fs) - estados.count("ok"),
                "n_distinct_forecasts": len(hashes),
                "deterministic": len(set(estados)) == 1 and len(hashes) <= 1,
            }
        )
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="imprime el plan y el coste estimado; no ajusta nada")
    ap.add_argument("--out", type=Path, default=None, help="JSON de resultados (obligatorio salvo --dry-run)")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--threads", default=",".join(CONFIGS), help="configuraciones: 1,default")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configs = tuple(c.strip() for c in args.threads.split(",") if c.strip())
    for c in configs:
        entorno(c, base={})  # valida antes de nada
    combos = cargar_combos()
    est = coste_estimado(combos, args.repeats, configs)
    print(
        f"{len(combos)} combinaciones × {len(configs)} configuraciones × {args.repeats} repeticiones "
        f"= {len(combos) * len(configs) * args.repeats} ajustes · coste estimado {est / 60:.0f} min (secuencial)"
    )
    if args.dry_run:
        for c in combos:
            print(f"  {c.table} {c.country}/{c.category} {c.model}  excluida en {', '.join(c.campaigns)}")
        return 0
    if args.out is None:
        raise SystemExit("--out es obligatorio: la sonda no escribe en reports/ por su cuenta")
    if (ROOT / "reports") in args.out.resolve().parents:
        raise SystemExit("--out apunta dentro de reports/: la sonda es diagnóstico, no un artefacto de la campaña")
    filas: list[dict] = []
    t0 = time.time()
    for rep in range(args.repeats):
        for combo in combos:
            for config in configs:
                r = ensayo(combo, config)
                filas.append({**asdict(combo), "threads": config, "repeat": rep, **r})
                print(
                    f"[{rep}] {combo.table} {combo.country}/{combo.category} {combo.model} hilos={config}: {r['status']}"
                )
    import numpy as np

    informe = {
        "schema": "linalg-probe/1",
        "registry": str(REGISTRY.relative_to(ROOT)),
        "env": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "blas": np.show_config(mode="dicts")["Build Dependencies"]["blas"].get("name"),
        },
        "repeats": args.repeats,
        "threads": list(configs),
        "seconds": round(time.time() - t0, 1),
        "summary": resumir(filas),
        "trials": filas,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(informe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

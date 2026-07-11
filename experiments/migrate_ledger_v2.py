"""Migración única de los ledgers prospectivos al esquema v2 (A2, plan auditoría 2026-07-11).

Añade a ``reports/prospective/forecast_log.csv`` y ``forecast_log_shadow.csv`` las columnas
de identidad de freeze (``vp_model.ledger.V2_COLS``) derivando el **acta de nacimiento de
cada fila del historial git**: el primer commit cuya versión del ledger contiene la fila
aporta ``frozen_at`` (fecha de commit, UTC) y ``git_sha``; y como el panel CSV también está
versionado, ``panel_hash`` y ``freeze_panel_vintage`` se recomputan **al estado de ese
commit**. ``evaluation_mode`` aplica entonces la misma regla que las filas nuevas:
``live`` solo si el mes objetivo es posterior al vintage del panel al congelar.

Esto ES la "cuarentena lógica" del plan: ninguna fila se borra; las que no pueden probar
un freeze anterior a su target quedan marcadas ``backfill`` y el scoring (A3) las separa.

Garantías:
- columnas originales byte-idénticas (verificado al final; si difieren, aborta sin escribir);
- idempotente: un ledger que ya trae columnas v2 no se re-migra (``--force`` para re-derivar);
- determinista: mismo historial git ⇒ misma salida.

Corre en ``ante`` desde la raíz:  ante/bin/python experiments/migrate_ledger_v2.py
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vp_model import champion, config, ledger  # noqa: E402

log = config.get_logger("migrate_ledger_v2")

# Rutas históricas de cada ledger (el reorg ea29910 del 3-jul-2026 movió reports/ a
# subdirectorios por rol; antes el ledger vivía en la raíz de reports/).
LEDGERS: dict[str, list[str]] = {
    "reports/prospective/forecast_log.csv": ["reports/prospective/forecast_log.csv", "reports/forecast_log.csv"],
    "reports/prospective/forecast_log_shadow.csv": ["reports/prospective/forecast_log_shadow.csv"],
}
PANEL_PATHS = ["data/processed/visa_panel_long.csv"]
MANIFEST_PATHS = ["reports/governance/champion_manifest.json", "reports/champion_manifest.json"]


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL, cwd=ROOT)


def _show(sha: str, paths: list[str]) -> bytes | None:
    """Contenido del primer path que exista en ese commit (tolera el reorg de rutas)."""
    for p in paths:
        try:
            return subprocess.check_output(["git", "show", f"{sha}:{p}"], stderr=subprocess.DEVNULL, cwd=ROOT)
        except subprocess.CalledProcessError:
            continue
    return None


def _commits(paths: list[str]) -> list[tuple[str, str]]:
    """(sha, fecha ISO UTC) de los commits que tocaron el ledger, del más viejo al más nuevo."""
    out = _git("log", "--reverse", "--format=%H|%cI", "--", *paths)
    commits = []
    for line in out.strip().splitlines():
        sha, iso = line.split("|", 1)
        ts = datetime.fromisoformat(iso).astimezone(UTC).isoformat(timespec="seconds")
        commits.append((sha, ts))
    return commits


def _panel_state(sha: str, cache: dict) -> tuple[str, str]:
    """(panel_hash, freeze_panel_vintage) del panel AL ESTADO de ese commit."""
    if sha not in cache:
        raw = _show(sha, PANEL_PATHS)
        if raw is None:
            cache[sha] = ("n/d", "n/d")
        else:
            import hashlib

            df = pd.read_csv(io.BytesIO(raw), usecols=["bulletin_date"])
            cache[sha] = (hashlib.md5(raw).hexdigest()[:12], str(df["bulletin_date"].max())[:7])
    return cache[sha]


def _recipes_at(sha: str, cache: dict) -> dict[str, str]:
    """Receta campeona por tabla según el manifiesto AL ESTADO de ese commit (o n/d)."""
    if sha not in cache:
        raw = _show(sha, MANIFEST_PATHS)
        if raw is None:
            cache[sha] = {}
        else:
            try:
                data = json.loads(raw)
                cache[sha] = {t: champion.recipe_from_dict(d).name for t, d in data.items()}
            except json.JSONDecodeError, KeyError, TypeError:
                cache[sha] = {}
    return cache[sha]


def migrate(path_key: str, force: bool = False) -> bool:
    path = ROOT / path_key
    if not path.exists():
        log.warning("%s no existe — nada que migrar", path_key)
        return False
    df = pd.read_csv(path)
    if "forecast_id" in df.columns and not force:
        log.info("%s ya está en v2 (%d filas) — no se re-migra", path_key, len(df))
        return False
    original = df.drop(columns=[c for c in ledger.V2_COLS if c in df.columns])

    is_shadow = "shadow" in path_key
    panel_cache: dict = {}
    recipe_cache: dict = {}
    key_of = lambda r: (str(r["origin"]), str(r["country"]), str(r["category"]), str(r["table"]), str(r["date"]))  # noqa: E731
    birth: dict[tuple, dict] = {}
    for sha, ts in _commits(LEDGERS[path_key]):
        raw = _show(sha, LEDGERS[path_key])
        if raw is None:
            continue
        hist = pd.read_csv(io.BytesIO(raw))
        phash, vintage = _panel_state(sha, panel_cache)
        recipes = _recipes_at(sha, recipe_cache)
        sha12 = sha[:12]
        for r in hist.to_dict("records"):
            k = key_of(r)
            if k in birth:
                continue
            mv = str(r.get("recipe", "n/d")) if is_shadow else recipes.get(str(r["table"]), "n/d")
            birth[k] = {
                "frozen_at": ts,
                "git_sha": sha12,
                "panel_hash": phash,
                "freeze_panel_vintage": vintage,
                "model_version": mv,
            }

    unmatched = 0
    stamps: list[dict] = []
    for r in df.to_dict("records"):
        b = birth.get(key_of(r))
        if b is None:  # fila aún sin commit (árbol sucio) — identidad del estado actual
            unmatched += 1
            b = {
                "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "git_sha": ledger.git_sha(),
                "panel_hash": ledger.panel_hash(),
                "freeze_panel_vintage": ledger.panel_vintage(),
                "model_version": str(r.get("recipe", "n/d")) if is_shadow else "n/d",
            }
        mode = "live" if str(r["date"])[:7] > b["freeze_panel_vintage"] else "backfill"
        row = {**r, **b, "evaluation_mode": mode}
        row["forecast_id"] = ledger.forecast_id(row)
        stamps.append(row)

    out = pd.DataFrame(stamps)[list(original.columns) + ledger.V2_COLS]
    # Garantía: las columnas originales quedan byte-idénticas o se aborta.
    if not out[original.columns].equals(original):
        raise SystemExit(f"ABORT {path_key}: la migración alteraría columnas originales — no se escribe")
    problems = ledger.validate(out)
    if problems:
        raise SystemExit(f"ABORT {path_key}: el resultado viola el contrato v2: {problems}")
    out.to_csv(path, index=False)
    modes = out["evaluation_mode"].value_counts().to_dict()
    vint_live = sorted(out.loc[out["evaluation_mode"] == "live", "origin"].unique())
    log.info(
        "%s → v2: %d filas · modos %s · añadas con filas live %s · sin acta git: %d",
        path_key,
        len(out),
        modes,
        vint_live,
        unmatched,
    )
    return True


def main() -> int:
    force = "--force" in sys.argv
    changed = [migrate(k, force=force) for k in LEDGERS]
    return 0 if any(changed) or all((ROOT / k).exists() for k in LEDGERS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

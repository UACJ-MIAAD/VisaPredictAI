"""
MEGA AUDIT — exhaustive, multi-dimensional data-quality audit of the VisaPredict
AI panel and its 10 source CSVs. Writes mega_audit_report.md.

Dimensions:
  1  Schema & dtypes
  2  Bulletin-level completeness (accordion vs dead months)
  3  Series inventory (length, span, gaps per series)
  4  Status distribution (F/C/U/UNK)
  5  (fusionada en 3: huecos por serie en el inventario)
  6  Key uniqueness
  7  Date validity (range, priority<=bulletin, base non-negative)
  8  DFF vs FAD coherence (DFF priority should be >= FAD priority)
  9  Jump anomalies (month-over-month, candidate parse errors)
  10 Source<->panel reconciliation
  11 Category coverage matrix
  12 Trainability preview (continuous F runs per series)

Run: ante/bin/python -m pipeline.mega_audit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from vp_data.config import CANONICAL_COUNTRY as COUNTRIES
from vp_data.config import DAYS_PER_YEAR, DEAD_MONTHS
from vp_data.config import PANEL_PATH as PANEL
from vp_data.config import RAW_DIR as RAW

OUT = Path("reports/governance/mega_audit_report.md")

SEVERITIES: tuple[str, ...] = ("CRIT", "WARN", "INFO")


@dataclass
class AuditReport:
    """C3: el informe es un objeto, no dos listas globales.

    Antes, `L` y `FLAGS` vivían a nivel de módulo: llamar a `main()` dos veces en el mismo
    proceso duplicaba líneas y banderas, así que la auditoría solo era correcta en un proceso
    recién arrancado. Aquí el estado pertenece a una instancia, y `main()` crea una por
    invocación. La semántica de severidades y el veredicto no cambian.
    """

    lines: list[str] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def flag(self, sev: str, msg: str) -> None:
        if sev not in SEVERITIES:
            raise ValueError(f"severidad desconocida: {sev!r} (esperada una de {SEVERITIES})")
        self.flags.append((sev, msg))

    def sec(self, n: int, title: str) -> None:
        self.add("", f"## {n}. {title}", "")

    def by_severity(self, sev: str) -> list[str]:
        return [m for s, m in self.flags if s == sev]

    @property
    def verdict(self) -> str:
        return "APTO" if not self.by_severity("CRIT") else "REQUIERE ATENCIÓN"

    def render(self) -> str:
        """El informe tal cual se escribe: mismo criterio de unión que antes."""
        return "\n".join(self.lines)

    @property
    def exit_code(self) -> int:
        """E5: la auditoría tiene dientes — con críticos, `make audit` / `make all` rompen."""
        return 1 if self.by_severity("CRIT") else 0


def load_panel():
    p = pd.read_csv(PANEL, parse_dates=["bulletin_date", "priority_date"])
    return p


# ---------------------------------------------------------------- 1. schema
def d1_schema(rep: AuditReport, p) -> None:
    rep.sec(1, "Esquema & dtypes")
    exp = [
        "country",
        "block",
        "category",
        "table",
        "bulletin_date",
        "status",
        "priority_date",
        "days_since_base",
        "raw_value",
    ]
    miss = [c for c in exp if c not in p.columns]
    rep.add(f"- Panel columnas: `{list(p.columns)}`")
    rep.add(f"- Faltantes vs esperadas: {miss or 'ninguna ✓'}")
    if miss:
        rep.flag("CRIT", f"panel sin columnas {miss}")
    # source CSVs
    rep.add("- CSVs fuente:")
    for slug in COUNTRIES:
        for suf, lvl in [("", "EB_level"), ("_family", "F_level")]:
            fp = RAW / f"{slug}{suf}_visa_backlog_timecourse.csv"
            df = pd.read_csv(fp)
            need = {lvl, "status", "raw_value", "table_type", "visa_bulletin_date"}
            mm = need - set(df.columns)
            if mm:
                rep.flag("CRIT", f"{fp.name} sin {mm}")
    rep.add(
        f"  {'✓ todas las fuentes con columnas requeridas' if not any(f[0] == 'CRIT' for f in rep.flags) else '✗ ver flags'}"
    )


# ------------------------------------------------------- 2. completeness
def d2_completeness(rep: AuditReport, p) -> None:
    rep.sec(2, "Completitud a nivel boletín")
    months = pd.period_range(p.bulletin_date.min(), p.bulletin_date.max(), freq="M")
    present = set(p.bulletin_date.dt.to_period("M").astype(str))
    allm = [str(m) for m in months]
    absent = [m for m in allm if m not in present]
    rep.add(f"- Span: **{allm[0]} → {allm[-1]}** ({len(allm)} meses)")
    rep.add(f"- Meses con ≥1 fila en el panel: **{len(present)}** ({100 * len(present) / len(allm):.1f}%)")
    rep.add(f"- Meses sin ninguna fila: {absent or 'ninguno'}")
    rep.add(f"- Muertos confirmados (404 + Wayback-only): `{DEAD_MONTHS}`")
    unexpected = [m for m in absent if m not in DEAD_MONTHS]
    if unexpected:
        # K5: un mes histórico desaparecido del panel es EXACTAMENTE la clase de
        # fallo que este audit prometió atrapar — CRIT (exit 1), no WARN.
        rep.flag("CRIT", f"meses ausentes no explicados: {unexpected}")
        rep.add(f"- ⚠️ Ausentes NO explicados: {unexpected}")
    else:
        rep.add("- ✓ Sin meses ausentes inexplicados.")


# ------------------------------------------------------- 3. series inventory
def series_table(p):
    g = p.groupby(["country", "block", "category", "table"])
    rows = []
    for key, d in g:
        d = d.sort_values("bulletin_date")
        per = d.bulletin_date.dt.to_period("M")
        span = pd.period_range(per.min(), per.max(), freq="M")
        gaps = len(set(span) - set(per))
        rows.append(
            {
                "country": key[0],
                "block": key[1],
                "category": key[2],
                "table": key[3],
                "n": len(d),
                "n_F": int((d.status == "F").sum()),
                "start": str(per.min()),
                "end": str(per.max()),
                "span": len(span),
                "gaps": gaps,
            }
        )
    return pd.DataFrame(rows)


def d3_inventory(rep: AuditReport, inv) -> None:
    rep.sec(3, "Inventario de series (país × categoría × tabla)")
    rep.add(f"- Total series: **{len(inv)}**")
    rep.add(f"- Filas: **{inv.n.sum():,}** · filas status=F: **{inv.n_F.sum():,}**")
    rep.add("- Por bloque×tabla:")
    bt = inv.groupby(["block", "table"]).agg(series=("n", "size"), filas=("n", "sum"), F=("n_F", "sum")).reset_index()
    rep.add("", "| bloque | tabla | series | filas | F |", "|---|---|--:|--:|--:|")
    for _, r in bt.iterrows():
        rep.add(f"| {r.block} | {r.table} | {r.series} | {r.filas:,} | {r.F:,} |")
    rep.add("", "- Series más cortas (n_F menor), candidatas a exclusión:")
    # C3: desempate determinista. `n_F` empata en decenas de series (todas las que nunca
    # publicaron una fecha), y `sort_values` usa quicksort, que NO es estable: con los mismos
    # datos en otro orden salían otras 12 filas. Se ordena por `n_F` y después por la clave
    # semántica completa, con `kind="stable"`, así que la selección solo depende de los datos.
    sml = inv.sort_values(["n_F", "country", "block", "category", "table"], kind="stable").head(12)
    rep.add("", "| país | cat | tabla | n | n_F | rango |", "|---|---|---|--:|--:|---|")
    for _, r in sml.iterrows():
        rep.add(f"| {r.country} | {r.category} | {r.table} | {r.n} | {r.n_F} | {r.start}→{r.end} |")


# ------------------------------------------------------- 4. status
def d4_status(rep: AuditReport, p) -> None:
    rep.sec(4, "Distribución de estado e∈{C,F,U,UNK}")
    vc = p.status.value_counts()
    rep.add("| status | filas | % |", "|---|--:|--:|")
    for s in ["F", "C", "U", "UNK"]:
        n = int(vc.get(s, 0))
        rep.add(f"| {s} | {n:,} | {100 * n / len(p):.1f}% |")
    na = int(vc.get("UNK", 0))
    if na > 0:
        rep.flag("WARN", f"{na} filas status=UNK (celdas no parseadas)")
    rep.add("", "- Por bloque×tabla (solo F / total):")
    rep.add("", "| bloque | tabla | F | total | %F |", "|---|---|--:|--:|--:|")
    for (b, t), d in p.groupby(["block", "table"]):
        f = int((d.status == "F").sum())
        rep.add(f"| {b} | {t} | {f:,} | {len(d):,} | {100 * f / len(d):.0f}% |")


# ------------------------------------------------------- 6. uniqueness
def d6_keys(rep: AuditReport, p) -> None:
    rep.sec(6, "Unicidad de clave")
    k = ["country", "block", "category", "table", "bulletin_date"]
    dup = int(p.duplicated(subset=k).sum())
    rep.add(f"- Claves (país,bloque,cat,tabla,mes) duplicadas: **{dup}** {'✓' if dup == 0 else '✗'}")
    if dup:
        rep.flag("CRIT", f"{dup} claves duplicadas en panel")


# ------------------------------------------------------- 7. date validity
def d7_validity(rep: AuditReport, p) -> None:
    rep.sec(7, "Validez de fechas")
    f = p[p.status == "F"]
    neg = int((p.days_since_base < 0).sum())
    rep.add(f"- `days_since_base` negativos: **{neg}** {'✓' if neg == 0 else '✗'}")
    if neg:
        rep.flag("CRIT", f"{neg} days_since_base negativos")
    # priority within plausible year range
    yr = f.priority_date.dt.year
    ceiling = int(p.bulletin_date.dt.year.max())  # techo = último boletín, no un año congelado
    bad_yr = f[(yr < 1975) | (yr > ceiling)]
    rep.add(f"- priority_date fuera de [1975,{ceiling}]: **{len(bad_yr)}** {'✓' if len(bad_yr) == 0 else '✗'}")
    if len(bad_yr):
        rep.flag("CRIT", f"{len(bad_yr)} priority_date fuera de [1975,{ceiling}]")  # E5: antes imprimía ✗ sin flag
    # priority should not exceed the bulletin month (final action can't be in the future)
    future = f[f.priority_date > f.bulletin_date]
    rep.add(f"- priority_date > bulletin_date (fecha futura): **{len(future)}**")
    if len(future):
        rep.flag("WARN", f"{len(future)} filas con priority_date posterior al boletín")
        ex = future.head(5)[["country", "category", "table", "bulletin_date", "priority_date", "raw_value"]]
        rep.add("", "  Ejemplos:", "")
        rep.add("  | país | cat | tabla | boletín | priority | raw |", "  |---|---|---|---|---|---|")
        for _, r in ex.iterrows():
            rep.add(
                f"  | {r.country} | {r.category} | {r.table} | {r.bulletin_date:%Y-%m} | {r.priority_date:%Y-%m-%d} | {r.raw_value} |"
            )
    # priority/status/raw consistency: every F has a parseable raw, every C/U has matching raw
    badC = int(((p.status == "C") & (p.raw_value.astype(str).str.upper() != "C")).sum())
    badU = int(((p.status == "U") & (p.raw_value.astype(str).str.upper() != "U")).sum())
    rep.add(f"- status=C con raw≠'C': {badC} · status=U con raw≠'U': {badU}")
    # E5: contrato central del panel (days_iff_F / pdate_iff_F + dominio de status) —
    # una fila status='X' o un F sin fecha eran invisibles para la auditoría.
    dom = int((~p.status.isin(["F", "C", "U", "UNK"])).sum())
    rep.add(f"- status fuera del dominio {{F,C,U,UNK}}: **{dom}** {'✓' if dom == 0 else '✗'}")
    if dom:
        rep.flag("CRIT", f"{dom} filas con status fuera de dominio")
    f_null = int(((p.status == "F") & (p.priority_date.isna() | p.days_since_base.isna())).sum())
    nf_val = int(((p.status != "F") & (p.priority_date.notna() | p.days_since_base.notna())).sum())
    rep.add(
        f"- contrato days_iff_F: F sin fecha/días **{f_null}** · no-F con fecha/días **{nf_val}** {'✓' if f_null + nf_val == 0 else '✗'}"
    )
    if f_null or nf_val:
        rep.flag("CRIT", f"contrato days_iff_F violado (F nulos={f_null}, no-F con valor={nf_val})")


# ------------------------------------------------------- 8. DFF vs FAD
def d8_dff_fad(rep: AuditReport, p) -> None:
    rep.sec(8, "Coherencia DFF vs FAD (DFF debe ser ≥ avanzada que FAD)")
    f = p[p.status == "F"]
    piv = f.pivot_table(
        index=["country", "block", "category", "bulletin_date"],
        columns="table",
        values="priority_date",
        aggfunc="first",
    )
    both = piv.dropna(subset=["FAD", "DFF"])
    viol = both[both["DFF"] < both["FAD"]]
    rep.add(f"- Pares (mismo país/cat/mes) con FAD y DFF: **{len(both):,}**")
    rep.add(f"- Violaciones DFF < FAD: **{len(viol)}** ({100 * len(viol) / max(len(both), 1):.2f}%)")
    rep.add(
        "- _Interpretación: inversiones reales de pocos días publicadas por el "
        "Depto. de Estado (los `raw_value` parsean bien), NO errores de parseo._"
    )
    if len(viol):
        rep.flag("INFO", f"{len(viol)} pares con DFF anterior a FAD (revisar)")
        ex = viol.reset_index().head(6)
        rep.add("", "  | país | bloque | cat | mes | FAD | DFF |", "  |---|---|---|---|---|---|")
        for _, r in ex.iterrows():
            rep.add(
                f"  | {r.country} | {r.block} | {r.category} | {r.bulletin_date:%Y-%m} | {r['FAD']:%Y-%m-%d} | {r['DFF']:%Y-%m-%d} |"
            )


# ------------------------------------------------------- 9. jumps
def d9_jumps(rep: AuditReport, p, thresh_years=8) -> None:
    rep.sec(9, f"Anomalías de salto (Δ priority_date > {thresh_years} años en 1 mes)")
    f = p[p.status == "F"].sort_values("bulletin_date")
    anomalies = []
    for key, d in f.groupby(["country", "block", "category", "table"]):
        d = d.sort_values("bulletin_date")
        dd = d.priority_date.diff().dt.days / DAYS_PER_YEAR
        big = d[abs(dd) > thresh_years]
        for idx in big.index:
            anomalies.append(
                (key, d.loc[idx, "bulletin_date"], dd.loc[idx], d.loc[idx, "priority_date"], d.loc[idx, "raw_value"])
            )
    rep.add(
        f"- Saltos |Δ| > {thresh_years} años: **{len(anomalies)}** (candidatos a error de parseo o retrogresión fuerte)"
    )
    rep.add(
        "- _Interpretación: los revisados son reales — transición EB-4 dic-2022 "
        "(`22JUN22`) y backlogs México F1/F3 de los 80s (`01JAN81`); el parseo de "
        "año de 2 dígitos es correcto (69-99→19xx, 00-68→20xx). El modelo deberá "
        "tolerar retrogresiones._"
    )
    if anomalies:
        rep.flag("INFO", f"{len(anomalies)} saltos grandes mes-a-mes")
        rep.add("", "  | país | cat | tabla | mes | Δaños | priority | raw |", "  |---|---|---|---|--:|---|---|")
        for key, bm, dy, pdte, raw in sorted(anomalies, key=lambda x: -abs(x[2]))[:15]:
            rep.add(f"  | {key[0]} | {key[2]} | {key[3]} | {bm:%Y-%m} | {dy:+.1f} | {pdte:%Y-%m-%d} | {raw} |")


# ------------------------------------------------------- 10. reconciliation
def d10_reconcile(rep: AuditReport, p) -> None:
    rep.sec(10, "Reconciliación fuente ↔ panel")
    # K5: reconciliar TODO el volumen por status, no solo F — perder miles de
    # filas C/U era invisible para esta dimensión (y el piso de filas del gate
    # las tolera). Umbral > 50 = CRIT: el único drop legítimo (dedup de clave
    # May-2022) es de una decena de filas.
    src_counts: dict[str, int] = {}
    for slug in COUNTRIES:
        for suf in ["", "_family"]:
            df = pd.read_csv(RAW / f"{slug}{suf}_visa_backlog_timecourse.csv")
            for status, n in df.status.value_counts().items():
                src_counts[str(status)] = src_counts.get(str(status), 0) + int(n)
    panel_counts = {str(k): int(v) for k, v in p.status.value_counts().items()}
    rep.add(f"- Filas por status en las 10 fuentes (suma): **{src_counts}**")
    rep.add(f"- Filas por status en el panel: **{panel_counts}**")
    for status in sorted(set(src_counts) | set(panel_counts)):
        diff = src_counts.get(status, 0) - panel_counts.get(status, 0)
        if diff < 0:
            rep.flag("CRIT", f"panel tiene más filas {status} que las fuentes (imposible): {-diff}")
        elif diff > 50:
            rep.flag("CRIT", f"el panel perdió {diff} filas {status} respecto a las fuentes (>> dedup esperado)")
        elif diff > 0:
            rep.add(f"- Diferencia {status}: {diff} (dedup de clave, esperado y pequeño)")


# ------------------------------------------------------- 11. coverage matrix
def d11_matrix(rep: AuditReport, inv) -> None:
    rep.sec(11, "Matriz de cobertura categoría × país (nº de tablas con datos)")
    m = inv.pivot_table(index="category", columns="country", values="table", aggfunc="count", fill_value=0)
    rep.add("", "| categoría | " + " | ".join(m.columns) + " |")
    rep.add("|---|" + "---|" * len(m.columns))
    for cat, r in m.iterrows():
        rep.add(f"| {cat} | " + " | ".join(str(int(x)) for x in r.values) + " |")


# ------------------------------------------------------- 12. trainability
def _max_f_run(dates) -> int:
    """Corrida máxima de meses F CONSECUTIVOS (huecos > 1 mes cortan la corrida)."""
    if len(dates) == 0:
        return 0
    d = sorted(pd.Timestamp(x).to_period("M") for x in dates)
    best = cur = 1
    for a, b in zip(d, d[1:], strict=False):
        cur = cur + 1 if (b - a).n == 1 else 1
        best = max(best, cur)
    return best


def d12_train(rep: AuditReport, inv, p) -> None:
    rep.sec(12, "Vista previa de entrenabilidad (F totales y corrida F continua máxima)")
    f = p[p.status == "F"]
    runs = f.groupby(["country", "block", "category", "table"]).bulletin_date.apply(lambda s: _max_f_run(s.values))
    for thr in [24, 60, 120]:
        n = int((inv.n_F >= thr).sum())
        r = int((runs >= thr).sum())
        # E5: n_F total ≠ entrenable — una serie rota en tramos pasaba como continua
        rep.add(f"- Series con ≥ {thr} obs F: **{n}** / {len(inv)} · con corrida F CONTINUA ≥ {thr}: **{r}**")
    rep.add(
        "",
        "- Las series con n_F bajo (EB-5 set-asides, categorías sin columna "
        "histórica) son cobertura **estructural**; el filtro evaluable/piloto "
        "del anteproyecto las descarta para modelado.",
    )


def build_report(p, inv=None) -> AuditReport:
    """Construye un informe COMPLETO sobre un panel dado. Sin estado global: cada llamada
    devuelve una instancia nueva, así que dos auditorías seguidas no se contaminan."""
    rep = AuditReport()
    rep.add(
        "# MEGA AUDIT — VisaPredict AI panel",
        "",
        "_Auditoría exhaustiva generada por `mega_audit.py` sobre `data/processed/visa_panel_long.csv` y las 10 fuentes._",
    )
    if inv is None:
        inv = series_table(p)
    d1_schema(rep, p)
    d2_completeness(rep, p)
    d3_inventory(rep, inv)
    d4_status(rep, p)
    d6_keys(rep, p)
    d7_validity(rep, p)
    d8_dff_fad(rep, p)
    d9_jumps(rep, p)
    d10_reconcile(rep, p)
    d11_matrix(rep, inv)
    d12_train(rep, inv, p)

    # veredicto
    crit, warn, info = (rep.by_severity(s) for s in SEVERITIES)
    rep.add("", "## Veredicto", "")
    rep.add(f"- 🔴 CRÍTICOS: **{len(crit)}**" + (": " + "; ".join(crit) if crit else " — ninguno ✓"))
    rep.add(f"- 🟡 ADVERTENCIAS: **{len(warn)}**" + (": " + "; ".join(warn) if warn else " — ninguna ✓"))
    rep.add(f"- 🔵 INFORMATIVOS: **{len(info)}**" + (": " + "; ".join(info) if info else ""))
    rep.add("", f"**Estado del panel: {rep.verdict}**")
    return rep


def main() -> int:
    rep = build_report(load_panel())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rep.render(), encoding="utf-8")
    print(f"✓ {OUT} ({len(rep.lines)} líneas)")
    print("\n".join(f"  [{s}] {m}" for s, m in rep.flags) or "  sin flags")
    crit, warn, info = (rep.by_severity(s) for s in SEVERITIES)
    print(f"\nVEREDICTO: {rep.verdict}  (crit={len(crit)} warn={len(warn)} info={len(info)})")
    return rep.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""M74-E-R9 · el sello de entradas se ACREDITA contra el manifiesto de su campaña.

Auditoría `e2429a0b…`: R8 emitía el preflight y anotaba su sha256, pero nadie lo volvía a abrir. Un
preflight de HEAD A con un manifiesto de HEAD B era publicable; un sello vacío con rc 0 se promovía
con el hash del vacío; y el preflight corría antes de fijar la identidad.
`campaign_manifest.seal_problems` es ahora la única función de ese vínculo, y la consumen el
runbook, el gate de completitud y la publicación.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import tools.campaign_manifest as cm  # noqa: E402
import tools.check_campaign_completeness as gate  # noqa: E402

RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"
SELLO_REL = "reports/logs/preflight_rederiv_aaaaaaa_20260914T000000.json"


# ═══════════════════════════════ la función única
def test_control_un_sello_legitimo_se_acredita(tmp_path: Path, campana_sellada) -> None:
    assert cm.seal_problems(campana_sellada(tmp_path)) == []


@pytest.mark.parametrize(
    "crudo,motivo", [(b"", "sello ilegible"), (b"{}", "no cumple las claves")], ids=["vacio", "llaves"]
)
def test_RED_un_sello_vacio_o_sin_esquema(tmp_path: Path, campana_sellada, crudo: bytes, motivo: str) -> None:
    assert any(motivo in x for x in cm.seal_problems(campana_sellada(tmp_path, sello=crudo)))


def test_RED_sello_ausente(tmp_path: Path, campana_sellada) -> None:
    m = campana_sellada(tmp_path)
    (tmp_path / SELLO_REL).unlink()
    assert any("ausente" in x for x in cm.seal_problems(m))


def test_RED_bytes_alterados_despues_del_manifiesto(tmp_path: Path, campana_sellada) -> None:
    m = campana_sellada(tmp_path)
    sello = tmp_path / SELLO_REL
    antes = sello.read_bytes()
    sello.write_bytes(antes + b" ")
    # ★ M74-E-R10: el `replace` de `"prueba"` dejó de cambiar nada al usar el sello real; se exige el cambio
    assert sello.read_bytes() != antes
    assert any("sha256 distinto" in x for x in cm.seal_problems(m))


def test_RED_hash_incorrecto_en_el_manifiesto(tmp_path: Path, campana_sellada) -> None:
    m = campana_sellada(tmp_path)
    d = json.loads(m.read_text(encoding="utf-8"))
    d["preflight_sha256"] = "0" * 64
    m.write_text(json.dumps(d), encoding="utf-8")
    assert any("sha256 distinto" in x for x in cm.seal_problems(m))


@pytest.mark.parametrize("campo,valor", [("head", "b" * 40), ("dirty", True)])
def test_RED_identidad_contradictoria(tmp_path: Path, campana_sellada, campo: str, valor) -> None:
    sello = campana_sellada.sello()
    sello["git"][campo] = valor
    assert any("otra identidad" in x for x in cm.seal_problems(campana_sellada(tmp_path, sello=sello)))


@pytest.mark.parametrize("campo,valor", [("preflight", "reports/logs/otro.json"), ("extra", 1)])
def test_RED_manifiesto_fuera_de_esquema_o_de_ruta(tmp_path: Path, campana_sellada, campo: str, valor) -> None:
    m = campana_sellada(tmp_path)
    d = json.loads(m.read_text(encoding="utf-8"))
    d[campo] = valor
    m.write_text(json.dumps(d), encoding="utf-8")
    assert cm.seal_problems(m)


def test_RED_sello_por_enlace_simbolico(tmp_path: Path, campana_sellada) -> None:
    m = campana_sellada(tmp_path)
    sello = tmp_path / SELLO_REL
    fuera = tmp_path / "fuera.json"
    fuera.write_bytes(sello.read_bytes())
    sello.unlink()
    sello.symlink_to(fuera)
    assert any("no regular" in x for x in cm.seal_problems(m))


def test_RED_entorno_que_no_reproduce_su_lock(tmp_path: Path, campana_sellada) -> None:
    sello = campana_sellada.sello()
    sello["environment"]["ante"]["reproduces_lock"] = False
    assert any("reproduces_lock" in x for x in cm.seal_problems(campana_sellada(tmp_path, sello=sello)))


def test_la_acreditacion_no_consulta_el_head_vivo(tmp_path: Path, monkeypatch, campana_sellada) -> None:
    """La validación posterior acredita la campaña sellada, no el checkout de hoy."""

    def prohibido(*_a, **_k):
        raise AssertionError("la acreditación consultó git")

    monkeypatch.setattr(subprocess, "run", prohibido)
    assert cm.seal_problems(campana_sellada(tmp_path, head="d" * 40)) == []


# ═══════════════════════════════ sus consumidores
def test_RED_la_publicacion_rechaza_un_preflight_de_otro_head(tmp_path: Path, campana_sellada) -> None:
    """★ El ataque de la auditoría: preflight de HEAD aaa…, manifiesto de bbb… con el hash CORRECTO."""
    m = campana_sellada(tmp_path, head="b" * 40, sello=campana_sellada.sello(head="a" * 40))
    assert "otra identidad" in (cm.publish_blocker(m) or "")
    assert cm.publish_blocker(campana_sellada(tmp_path / "control")) is None


def test_RED_el_gate_de_completitud_consume_la_acreditacion(tmp_path: Path, monkeypatch, campana_sellada) -> None:
    monkeypatch.setattr(gate, "MANIFEST", campana_sellada(tmp_path))
    assert not [p for p in gate.check("outputs") if p.startswith("SELLO")]
    (tmp_path / SELLO_REL).write_bytes(b"{}")
    assert [p for p in gate.check("outputs") if p.startswith("SELLO")]


def test_cli_assert_sealed(tmp_path: Path, campana_sellada) -> None:
    assert cm.main(["prog", "--assert-sealed", str(campana_sellada(tmp_path / "ok"))]) == 0
    assert cm.main(["prog", "--assert-sealed", str(campana_sellada(tmp_path / "mal", sello=b""))]) == 1


# ═══════════════════════════════ el arranque REAL, en un repositorio temporal
_PYTHON_STUB = """#!/bin/sh
case "$*" in
  *campaign_preflight*)
    [ "$SELLO" = rc1 ] && exit 1
    out=""; previo=""; for a in "$@"; do [ "$previo" = --out ] && out="$a"; previo="$a"; done
    case "$SELLO" in
      vacio) : ;;
      llaves) echo "{}" > "$out" ;;
      commit:*) git -c user.name=p -c user.email=p@example.invalid -c core.hooksPath=/dev/null commit -q --allow-empty -m movido
                sed "s/__HEAD__/$(git rev-parse HEAD)/" "${SELLO#commit:}" > "$out" ;;
      *) sed "s/__HEAD__/$(git rev-parse HEAD)/" "$SELLO" > "$out" ;;
    esac ;;
  *campaign_manifest*) PYTHONPATH="$RAIZ_REAL" exec "$PY_REAL" "$@" ;;
esac
exit 0
"""


def _ejecutable(ruta: Path, texto: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto, encoding="utf-8")
    ruta.chmod(ruta.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _arranque(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    guion = RUNBOOK.read_text(encoding="utf-8")
    bloque = guion[guion.index("set -uo pipefail") : guion.index("# ── Transacción de campaña")]
    cola = "\n: > reports/campaign/PRIMER_PRODUCTOR\nexit 0\n"
    _ejecutable(repo / "experiments" / "arranque.sh", "#!/bin/bash\n" + bloque + cola)
    for venv in ("ante", "ante_nf"):
        _ejecutable(repo / venv / "bin" / "python", _PYTHON_STUB)
    git = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=p",
        "-c",
        "user.email=p@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "arranque"]):
        subprocess.run([*git, *args], check=True, capture_output=True)
    return repo


def _plantilla(tmp_path: Path, campana_sellada, **git) -> Path:
    sello = campana_sellada.sello(head="__HEAD__")
    sello["git"].update(git)
    ruta = tmp_path / "plantilla.json"
    ruta.write_text(json.dumps(sello), encoding="utf-8")
    return ruta


def _correr(repo: Path, tmp_path: Path, sello: str, shim: str | None = None) -> subprocess.CompletedProcess:
    (tmp_path / "tmp").mkdir(exist_ok=True)
    ruta = "/usr/bin:/bin"
    if shim:
        _ejecutable(tmp_path / "shim" / shim, "#!/bin/sh\nexit 1\n")
        ruta = f"{tmp_path / 'shim'}:{ruta}"
    entorno = {"PATH": ruta, "HOME": str(tmp_path), "TMPDIR": str(tmp_path / "tmp"), "SELLO": sello}
    entorno |= {"RAIZ_REAL": str(RAIZ), "PY_REAL": sys.executable}
    guion = str(repo / "experiments" / "arranque.sh")
    return subprocess.run(["bash", guion], capture_output=True, text=True, timeout=120, env=entorno)


@pytest.mark.parametrize(
    "caso,rc",
    [
        ("vacio", 13),
        ("llaves", 13),
        ("rc1", 11),
        ("head", 13),
        ("dirty", 13),
        ("commit", 13),
        ("mv", 12),
        ("shasum", 12),
    ],
)
def test_RED_el_arranque_real_no_alcanza_el_primer_productor(
    tmp_path: Path, campana_sellada, caso: str, rc: int
) -> None:
    """★ Sello vacío o `{}` con rc 0, preflight roto, otro HEAD, otro dirty, HEAD que se mueve mientras se
    sella, y fallos de promoción o de hash: ninguno llega al primer productor."""
    repo = _arranque(tmp_path)
    if caso in ("vacio", "llaves", "rc1"):
        sello = caso
    elif caso == "head":
        sello = str(_plantilla(tmp_path, campana_sellada, head="c" * 40))
    elif caso == "dirty":
        sello = str(_plantilla(tmp_path, campana_sellada, dirty=True))
    elif caso == "commit":
        sello = "commit:" + str(_plantilla(tmp_path, campana_sellada))
    else:
        sello = str(_plantilla(tmp_path, campana_sellada))
    fin = _correr(repo, tmp_path, sello, shim=caso if caso in ("mv", "shasum") else None)
    assert fin.returncode == rc, fin.stdout + fin.stderr
    assert not (repo / "reports" / "campaign" / "PRIMER_PRODUCTOR").exists()


def test_control_el_arranque_real_con_un_sello_legitimo(tmp_path: Path, campana_sellada) -> None:
    repo = _arranque(tmp_path)
    fin = _correr(repo, tmp_path, str(_plantilla(tmp_path, campana_sellada)))
    assert fin.returncode == 0, fin.stdout + fin.stderr
    assert (repo / "reports" / "campaign" / "PRIMER_PRODUCTOR").exists()
    manifiesto = repo / "reports" / "campaign" / "campaign_manifest.json"
    assert cm.seal_problems(manifiesto) == []
    cabeza = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert json.loads(manifiesto.read_text(encoding="utf-8"))["git_sha"] == cabeza


def test_la_identidad_se_fija_antes_de_emitir_el_sello() -> None:
    vivo = "\n".join(ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    i_sello = vivo.index("-m tools.campaign_preflight")
    assert vivo.index('[ -n "$(tree_dirty)" ]') < vivo.index('CAMPAIGN_SHA="$(git rev-parse HEAD)"') < i_sello
    assert i_sello < vivo.index("--assert-sealed") < vivo.index("txn archive")

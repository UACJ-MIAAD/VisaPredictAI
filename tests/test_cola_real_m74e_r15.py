"""M74-E-R15 · lo que la auditoría ciega read-only sobre `7a03429` encontró, y lo que R14 dejó a medias.

Bloqueo B1: la cola real del runbook sale con `exit 2` cuando el guardián de consistencia rompe —el
desenlace ESPERADO de una re-derivación— y ese `exit` disparaba `campaign_abort`, que marcaba
`failed` una transacción ya `computed` (transición legal). Dieciséis horas terminaban en un estado
terminal con la consistencia pendiente. Las pruebas que había ensayaban una cola sintética que esperaba
`failed`: era vacua respecto del camino real.

Tres límites del mismo informe, cerrados aquí: `sync_all.sh --publish` invocaba `dvc push` a secas;
`dvc add models` arrastraba `models/.retired/*` al puntero; y `kill_descendants` mataba también el `tee`
de la bitácora. Y un defecto de la propia R14: su limpieza de pruebas borraba directorios servidos.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from tools import campaign_state as cs  # noqa: E402
from tools.check_dvc_lock_fresh import resolve_dvc  # noqa: E402

RUNBOOK = RAIZ / "experiments" / "run_rederivation.sh"
REAL_DVC = resolve_dvc(RAIZ, os.environ)


def _bloque(guion: str, ini: str, fin: str) -> str:
    a = guion.index(ini)
    b = guion.index(fin, a) + len(fin)
    return guion[a:b]


def _ensayo_cola_real(tmp_path: Path, consistency_ok: int, *, senal: str | None = None) -> tuple[int, dict | None]:
    """Bloque de transacción REAL + cola REAL del runbook, con el desenlace de consistencia elegido."""
    guion = RUNBOOK.read_text(encoding="utf-8")
    bloque = _bloque(guion, "# ── Transacción de campaña", "trap 'exit 129' HUP")
    cola = guion[guion.index("CONSISTENCY_STATE=") : guion.rindex("exit 0") + len("exit 0")] + "\n"
    if senal:
        # tras `txn compute`, una señal ya no debe tocar la transacción: se inyecta justo después
        cola = cola.replace(
            'if [ "$CONSISTENCY_OK" = 0 ]; then', f'kill -{senal} $$\nsleep 1\nif [ "$CONSISTENCY_OK" = 0 ]; then', 1
        )
    txn = tmp_path / "campaign.json"
    ensayo = tmp_path / "ensayo.sh"
    ensayo.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        f'ANTE="{sys.executable}"\nCAMPAIGN_ID="rederiv_aaaaaaa_20260915T000000"\nCAMPAIGN_SHA="{"a" * 40}"\n'
        f'CAMPAIGN_DIRTY="false"\nCAMPAIGN_TXN="{txn}"\nPREFLIGHT_SHA256="{"e" * 64}"\n'
        + bloque
        + f"\nCONSISTENCY_OK={consistency_ok}\nBEST_EFFORT_FAILED=()\n"
        + cola,
        encoding="utf-8",
    )
    panel = tmp_path / "panel.csv"
    panel.write_text("serie,mes,valor\n", encoding="utf-8")
    fin = subprocess.run(
        ["bash", str(ensayo)],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=600,
        env={
            **os.environ,
            "PYTHONPATH": str(RAIZ),
            "CAMPAIGN_TXN_ARCHIVE": str(tmp_path / "transactions"),
            "CAMPAIGN_TXN_PANEL": str(panel),
        },
    )
    return fin.returncode, cs.read(txn)


# ═══════════════════════════════ 1 · el desenlace esperado no es terminal
def test_RED_la_consistencia_rota_deja_computed_pending_con_exit_2(tmp_path: Path) -> None:
    """Contra `7a03429`: `status: failed`, `exit_code: 2`, `failed_stage: salida anormal del runbook`."""
    rc, obj = _ensayo_cola_real(tmp_path, 0)
    assert rc == 2
    assert obj is not None and obj["status"] == "computed" and obj["consistency"] == "pending", obj
    assert "failed_stage" not in obj or obj["failed_stage"] is None


def test_la_consistencia_limpia_deja_computed_passed_con_exit_0(tmp_path: Path) -> None:
    rc, obj = _ensayo_cola_real(tmp_path, 1)
    assert rc == 0
    assert obj is not None and obj["status"] == "computed" and obj["consistency"] == "passed"


@pytest.mark.parametrize("senal", ["INT", "TERM"])
def test_una_senal_despues_de_computed_ya_no_toca_la_transaccion(tmp_path: Path, senal: str) -> None:
    """Antes de `txn compute` los traps registran; después, la campaña ya terminó de calcular."""
    rc, obj = _ensayo_cola_real(tmp_path, 1, senal=senal)
    assert obj is not None and obj["status"] == "computed", (rc, obj)


def test_el_trap_sigue_registrando_una_salida_anormal_ANTES_de_computed(tmp_path: Path) -> None:
    """Control: desarmar los traps sólo después de `compute` no debilita el registro previo."""
    guion = RUNBOOK.read_text(encoding="utf-8")
    bloque = _bloque(guion, "# ── Transacción de campaña", "trap 'exit 129' HUP")
    txn = tmp_path / "campaign.json"
    ensayo = tmp_path / "ensayo.sh"
    ensayo.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        f'ANTE="{sys.executable}"\nCAMPAIGN_ID="rederiv_aaaaaaa_20260915T000000"\nCAMPAIGN_SHA="{"a" * 40}"\n'
        f'CAMPAIGN_DIRTY="false"\nCAMPAIGN_TXN="{txn}"\nPREFLIGHT_SHA256="{"e" * 64}"\n' + bloque + "\nexit 5\n",
        encoding="utf-8",
    )
    panel = tmp_path / "panel.csv"
    panel.write_text("serie,mes,valor\n", encoding="utf-8")
    fin = subprocess.run(
        ["bash", str(ensayo)],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=600,
        env={
            **os.environ,
            "PYTHONPATH": str(RAIZ),
            "CAMPAIGN_TXN_ARCHIVE": str(tmp_path / "t"),
            "CAMPAIGN_TXN_PANEL": str(panel),
        },
    )
    obj = cs.read(txn)
    assert fin.returncode == 5 and obj is not None and obj["status"] == "failed" and obj["exit_code"] == 5


def test_los_traps_se_desarman_despues_de_compute_y_no_antes() -> None:
    vivas = "\n".join(ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert vivas.count("trap - EXIT INT TERM HUP") == 1
    assert vivas.index("txn compute") < vivas.index("trap - EXIT INT TERM HUP") < vivas.index("exit 2")


# ═══════════════════════════════ 2 · el publicador también usa el DVC gobernado
def test_RED_el_publicador_no_invoca_ningun_dvc_a_secas() -> None:
    vivas = [
        ln
        for ln in (RAIZ / "experiments" / "sync_all.sh").read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    a_secas = [ln for ln in vivas if re.match(r"^\s*dvc\s", ln)]
    assert not a_secas, a_secas
    assert any('"$DVC" push' in ln for ln in vivas) and any('"$DVC" add' in ln for ln in vivas)


# ═══════════════════════════════ 3 · lo retirado no entra al puntero
@pytest.mark.skipif(REAL_DVC is None, reason="DVC gobernado (ante/bin/dvc o $VP_DVC) ausente")
def test_RED_dvc_add_models_excluye_el_arbol_retirado(tmp_path: Path) -> None:
    """Contra `7a03429`: `nfiles: 2`; el puntero de la campaña nueva arrastraba la añada anterior."""
    repo = tmp_path / "repo"
    (repo / "models" / ".retired" / "x").mkdir(parents=True)
    (repo / "models" / "a.txt").write_text("a", encoding="utf-8")
    (repo / "models" / ".retired" / "x" / "b.txt").write_text("b", encoding="utf-8")
    shutil.copy(RAIZ / ".dvcignore", repo / ".dvcignore")
    env = {**os.environ, "DVC_NO_ANALYTICS": "1"}
    subprocess.run([str(REAL_DVC), "init", "--no-scm", "-q"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run([str(REAL_DVC), "add", "models", "-q"], cwd=repo, check=True, capture_output=True, env=env)
    puntero = (repo / "models.dvc").read_text(encoding="utf-8")
    assert re.search(r"nfiles:\s*1\b", puntero), puntero


# ═══════════════════════════════ 4 · el trap conserva el tee de la bitácora
def test_RED_kill_descendants_conserva_el_tee_indicado(tmp_path: Path) -> None:
    guion = RUNBOOK.read_text(encoding="utf-8")
    funcion = re.search(r"^kill_descendants\(\) \{\n.*?^\}\n", guion, flags=re.M | re.S)
    assert funcion
    pids = tmp_path / "pids"
    log = tmp_path / "log"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    ensayo = tmp_path / "ensayo.sh"
    ensayo.write_text(
        "#!/bin/bash\n"
        + funcion.group(0)
        + f'tee -a "{log}" < "{fifo}" &\nTEE=$!\nexec 3>"{fifo}"\n'
        + f'sleep 300 & echo "$!" >> "{pids}"\n'
        + 'sleep 0.3\nkill_descendants "$$" "$TEE"\nsleep 0.3\n'
        + f'kill -0 "$TEE" && echo "tee vivo" >> "{log}.estado" || echo "tee muerto" >> "{log}.estado"\n'
        + 'exec 3>&-\nwait "$TEE" 2>/dev/null\nexit 0\n',
        encoding="utf-8",
    )
    subprocess.run(["bash", str(ensayo)], check=True, timeout=30)
    for pid in (int(x) for x in pids.read_text(encoding="utf-8").split()):
        try:
            os.kill(pid, 0)
            os.kill(pid, 9)
            pytest.fail(f"descendiente {pid} seguía vivo")
        except ProcessLookupError:
            pass
    assert (tmp_path / "log.estado").read_text(encoding="utf-8").strip() == "tee vivo"


# ═══════════════════════════════ 5 · las pruebas de R14 no destruyen el árbol servido
def test_la_limpieza_de_pruebas_solo_retira_lo_que_no_existia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(RAIZ / "tests"))
    import test_camino_real_m74e_r14 as r14

    monkeypatch.setattr(r14, "RAIZ", tmp_path)
    existente = tmp_path / "models" / "FAD" / "global" / "BiTCN"
    existente.mkdir(parents=True)
    (existente / "model.bin").write_bytes(b"servido")
    previos = r14._servidos(("BiTCN", "Nuevo"))
    nuevo = tmp_path / "models" / "DFF" / "global" / "Nuevo"
    nuevo.mkdir(parents=True)
    r14._limpiar_servido(("BiTCN", "Nuevo"), previos)
    assert (existente / "model.bin").read_bytes() == b"servido"
    assert not nuevo.exists()

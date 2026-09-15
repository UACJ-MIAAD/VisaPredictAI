"""M73 · La transacción de campaña, conducida de verdad (ADR 0003 · pendiente #56).

`tools/campaign_state.py` estaba probado como biblioteca desde julio. Lo que faltaba —y lo que
aquí se prueba— es la **conducción**: que un runner abra la transacción, que un fallo a mitad o
una interrupción la dejen en un estado que bloquea publicar, que dos procesos no puedan sellar la
misma campaña, y que el publicador se niegue ante cualquier estado que no sea ``validated``.

Todo corre sobre **lanes sintéticos en un directorio temporal**: un runbook de juguete que escribe
un artefacto por lane y conduce la transacción con el MISMO envoltorio
(`tools/campaign_txn.py`) que el runbook real. No se lanza ninguna campaña de verdad.

La frontera se dice en voz alta: el cableado de los dos guiones de shell reales
(`experiments/run_rederivation.sh` y `experiments/sync_all.sh`) se comprueba por inspección de su
texto, no ejecutándolos — ejecutarlos sería correr una campaña de horas y, en el segundo caso,
publicar. Esas pruebas van al final y están rotuladas como lo que son.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools import campaign_state as cs
from tools import campaign_txn as txn

RAIZ = Path(__file__).resolve().parents[1]
SHA = "a" * 40
SELLO = "e" * 64  # ★ M74-E-R12 · sha256 del sello de entradas comparado en vivo


# ----------------------------------------------------------------- andamiaje de lanes sintéticos
@pytest.fixture
def lanes(tmp_path: Path, campana_sellada) -> Path:
    """Un 'panel' de juguete, un sitio donde los lanes dejan sus artefactos y su manifiesto acreditado.

    ★ M74-E-R13: `guard` y `publish` exigen el manifiesto; la identidad de la campaña de juguete sale de él.
    """
    campana_sellada(tmp_path)
    (tmp_path / "panel.csv").write_text("country,category,table,value\nmexico,EB2,FAD,1\n", encoding="utf-8")
    return tmp_path


def _txn(raiz: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoca el envoltorio como lo hace un runner de shell: por su CLI, en un proceso aparte."""
    return subprocess.run(
        [sys.executable, "-m", "tools.campaign_txn", "--path", str(raiz / "reports/campaign/campaign.json"), *args],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(RAIZ)},
    )


def _recibo(raiz: Path, **cambios: str) -> Path:
    """Un recibo de revisión humana VÁLIDO para la campaña abierta en `raiz`, salvo lo que se mute.

    Se deriva del estado real —`campaign_id`, SHA de origen y hash del panel— porque ése es el
    punto: el recibo ya no puede ser un archivo cualquiera con tres argumentos tecleados.
    """
    estado = cs.read(raiz / "reports/campaign/campaign.json") or {}
    acta = {
        "schema": txn.RECEIPT_SCHEMA,
        "campaign_id": estado.get("campaign_id", "sintetica_0001"),
        "source_git_sha": estado.get("source_git_sha", SHA),
        "panel_sha256": estado.get("panel_sha256", ""),
        "input_seal_sha256": estado.get("input_seal_sha256", ""),
        "reviewed_by": "Javier Rebull",
        "decision": "aprobada",
        "reviewed_at": txn.now_rfc3339(),
    }
    acta.update(cambios)
    destino = raiz / f"recibo_{len(list(raiz.glob('recibo_*.json')))}.json"
    destino.write_text(json.dumps(acta, ensure_ascii=False, indent=2), encoding="utf-8")
    return destino


def _manifiesto(raiz: Path) -> Path:
    return raiz / "reports/campaign/campaign_manifest.json"


def _identidad(raiz: Path) -> dict:
    return json.loads(_manifiesto(raiz).read_text(encoding="utf-8"))


def _abrir(raiz: Path, campaign_id: str | None = None) -> subprocess.CompletedProcess[str]:
    """Abre con la identidad del manifiesto de `raiz` (M74-E-R13); `campaign_id` sólo para campañas sucesivas."""
    ident = _identidad(raiz)
    return _txn(raiz, "open", "--campaign-id", campaign_id or ident["campaign_id"], "--sha", ident["git_sha"],
                "--dirty", "false", "--panel", str(raiz / "panel.csv"),
                "--input-seal", ident["preflight_sha256"])  # fmt: skip


def _estado(raiz: Path) -> str | None:
    obj = cs.read(raiz / "reports/campaign/campaign.json")
    return None if obj is None else str(obj["status"])


def _leer(ruta: str | Path) -> dict:
    """`cs.read` devuelve `dict | None`; aquí la ausencia es un fallo de la prueba, no un caso."""
    obj = cs.read(ruta)
    assert obj is not None, f"no hay transacción legible en {ruta}"
    return obj


RUNBOOK = textwrap.dedent(
    """\
    #!/bin/bash
    # Runbook sintético: conduce la transacción con el MISMO envoltorio que el real.
    set -uo pipefail
    RAIZ="$1"; LANES="$2"; FALLA_EN="${3:-0}"; DUERME="${4:-0}"
    TXN="$RAIZ/reports/campaign/campaign.json"
    txn() { "$PYBIN" -m tools.campaign_txn --path "$TXN" "$@"; }
    txn open --campaign-id sintetica_0001 --sha AAAA --dirty false --panel "$RAIZ/panel.csv" \\
        --input-seal eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee || exit 7
    campaign_abort() {
      local rc=$?
      [ "$rc" -eq 0 ] && return 0
      txn fail --if-open --stage "salida anormal del runbook" --exit-code "$rc" \\
          --reason "el runbook terminó con exit $rc sin alcanzar un estado terminal" >&2 || true
    }
    trap campaign_abort EXIT
    trap 'exit 143' TERM
    REQ_FAILS=0
    for i in $(seq 1 "$LANES"); do
      [ "$DUERME" != 0 ] && sleep "$DUERME"
      if [ "$i" = "$FALLA_EN" ]; then
        REQ_FAILS=$((REQ_FAILS+1))
        echo "lane $i FALLA"
        break
      fi
      echo "lane $i ok" > "$RAIZ/reports/campaign/lane_$i.txt"
    done
    if [ "$REQ_FAILS" -gt 0 ]; then
      txn fail --if-open --stage "etapas obligatorias" --exit-code 1 \\
          --reason "$REQ_FAILS lane(s) obligatorio(s) roto(s)" >&2
      exit 1
    fi
    txn compute --input-gate passed --output-gate passed --consistency passed || exit 7
    exit 0
    """
)


@pytest.fixture
def runbook(lanes: Path) -> Path:
    guion = lanes / "runbook.sh"
    ident = _identidad(lanes)  # ★ M74-E-R13: la campaña de juguete describe el manifiesto acreditado
    texto = RUNBOOK.replace("AAAA", ident["git_sha"]).replace("sintetica_0001", ident["campaign_id"])
    texto = texto.replace(SELLO, ident["preflight_sha256"])
    assert SELLO not in texto and "sintetica_0001" not in texto, "la plantilla del runbook de juguete cambió"
    guion.write_text(texto, encoding="utf-8")
    guion.chmod(0o755)
    return guion


def _correr(runbook: Path, raiz: Path, lanes_n: int, falla_en: int = 0, duerme: float = 0, **kw):
    return subprocess.run(
        ["bash", str(runbook), str(raiz), str(lanes_n), str(falla_en), str(duerme)],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RAIZ), "PYBIN": sys.executable},
        **kw,
    )


# =============================================================== 1 · la corrida que sale bien
def test_una_corrida_limpia_llega_a_computed_y_todavia_no_autoriza_publicar(runbook, lanes) -> None:
    fin = _correr(runbook, lanes, 5, timeout=300)
    assert fin.returncode == 0, fin.stderr
    assert sorted(p.name for p in (lanes / "reports/campaign").glob("lane_*.txt")) == [
        f"lane_{i}.txt" for i in range(1, 6)
    ]
    assert _estado(lanes) == "computed"
    # ★ `computed` NO publica: falta la validación humana. El éxito técnico no es permiso.
    assert _txn(lanes, "guard", "--manifest", str(_manifiesto(lanes))).returncode == txn.EXIT_BLOCKED


def test_solo_validated_autoriza_publicar(runbook, lanes) -> None:
    assert _correr(runbook, lanes, 3, timeout=300).returncode == 0
    recibo = _recibo(lanes)
    fin = _txn(lanes, "validate", "--receipt", str(recibo))
    assert fin.returncode == 0, fin.stderr
    assert _estado(lanes) == "validated"
    assert _txn(lanes, "guard", "--manifest", str(_manifiesto(lanes))).returncode == txn.EXIT_OK
    # el hash del recibo se DERIVA del archivo, no se teclea
    obj = _leer(lanes / "reports/campaign/campaign.json")
    assert obj["validation_receipt_sha256"] == txn.sha256_file(recibo)
    # y publicar cierra el ciclo… dejando de autorizar publicar otra vez
    assert _txn(lanes, "publish", "--release-sha", "b" * 40, "--manifest", str(_manifiesto(lanes))).returncode == 0
    assert _estado(lanes) == "published"
    assert _txn(lanes, "guard", "--manifest", str(_manifiesto(lanes))).returncode == txn.EXIT_BLOCKED


# =============================================================== 2 · fallo a mitad de la corrida
def test_un_fallo_intermedio_deja_la_campana_en_failed_y_bloquea(runbook, lanes) -> None:
    fin = _correr(runbook, lanes, 5, falla_en=3, timeout=300)
    assert fin.returncode == 1
    # los lanes anteriores YA escribieron: la transacción gobierna el permiso, no deshace ficheros
    assert sorted(p.name for p in (lanes / "reports/campaign").glob("lane_*.txt")) == ["lane_1.txt", "lane_2.txt"]
    obj = _leer(lanes / "reports/campaign/campaign.json")
    assert obj["status"] == "failed" and obj["failed_stage"] == "etapas obligatorias"
    assert "1 lane(s)" in obj["reason"] and obj["exit_code"] == 1
    assert _txn(lanes, "guard", "--manifest", str(_manifiesto(lanes))).returncode == txn.EXIT_BLOCKED


def test_el_trap_no_tapa_el_fallo_ya_registrado(runbook, lanes) -> None:
    """RED de un defecto fácil: el trap se dispara DESPUÉS del `fail` explícito.

    Sin `--if-open`, intentaría `failed -> failed`, la máquina lo rechazaría con razón y el
    runbook moriría con un error espurio que taparía la causa verdadera. Aquí se exige que la
    razón registrada siga siendo la del fallo real, no la del trap.
    """
    _correr(runbook, lanes, 4, falla_en=2, timeout=300)
    obj = _leer(lanes / "reports/campaign/campaign.json")
    assert obj["failed_stage"] == "etapas obligatorias"
    assert "salida anormal" not in obj["reason"]
    assert obj["revision"] == 1, "sólo UNA transición debió escribirse"


# =============================================================== 3 · aborto e interrupción
def test_un_sigkill_a_mitad_deja_running_y_running_no_publica(runbook, lanes) -> None:
    """Un `SIGKILL` no ejecuta traps: la campaña queda `running`, y `running` bloquea."""
    proc = subprocess.Popen(
        ["bash", str(runbook), str(lanes), "8", "0", "0.4"],
        cwd=RAIZ,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(RAIZ), "PYBIN": sys.executable},
    )
    ruta = lanes / "reports/campaign/campaign.json"
    _esperar(lambda: ruta.exists() and any((lanes / "reports/campaign").glob("lane_*.txt")))
    proc.kill()
    proc.wait(timeout=60)
    assert _estado(lanes) == "running"
    assert _txn(lanes, "guard", "--manifest", str(_manifiesto(lanes))).returncode == txn.EXIT_BLOCKED
    # y no se puede «reiniciar encima»: sellar otra vez aborta
    assert _abrir(lanes).returncode == txn.EXIT_ERROR


def test_un_sigterm_a_mitad_se_registra_como_fallo(runbook, lanes) -> None:
    """A diferencia del `SIGKILL`, un `SIGTERM` sí deja que el trap escriba la causa."""
    proc = subprocess.Popen(
        ["bash", str(runbook), str(lanes), "8", "0", "0.4"],
        cwd=RAIZ,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(RAIZ), "PYBIN": sys.executable},
    )
    _esperar(lambda: any((lanes / "reports/campaign").glob("lane_*.txt")))
    proc.terminate()
    proc.wait(timeout=120)
    obj = _leer(lanes / "reports/campaign/campaign.json")
    assert obj["status"] == "failed" and "salida anormal" in obj["failed_stage"]
    assert obj["exit_code"] == 143
    assert _txn(lanes, "guard", "--manifest", str(_manifiesto(lanes))).returncode == txn.EXIT_BLOCKED


def _esperar(condicion, limite: float = 120.0) -> None:
    import time

    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        if condicion():
            return
        time.sleep(0.05)
    raise AssertionError("la condición no se cumplió a tiempo")


# =============================================================== 4 · concurrencia
def _lanzar(ordenes, entorno):
    procesos = [
        subprocess.Popen(o, cwd=RAIZ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=entorno)
        for o in ordenes
    ]
    return [p.wait(timeout=300) for p in procesos]


def test_dos_lanzamientos_simultaneos_y_solo_uno_sella_la_campana(lanes) -> None:
    """`O_EXCL`: exactamente uno crea la transacción; el resto aborta sin pisarla."""
    ruta = str(lanes / "reports/campaign/campaign.json")
    ordenes = [
        [
            sys.executable,
            "-m",
            "tools.campaign_txn",
            "--path",
            ruta,
            "open",
            "--campaign-id",
            f"simultanea_{i}",
            "--sha",
            SHA,
            "--dirty",
            "false",
            "--panel",
            str(lanes / "panel.csv"),
            "--input-seal",
            SELLO,
        ]  # fmt: skip
        for i in range(6)
    ]
    codigos = _lanzar(ordenes, {**os.environ, "PYTHONPATH": str(RAIZ)})
    assert codigos.count(0) == 1, f"debía sellar exactamente uno: {codigos}"
    obj = _leer(ruta)
    assert obj["status"] == "running" and obj["campaign_id"].startswith("simultanea_")
    assert obj["revision"] == 0 and cs.validate_schema(obj) == []


def test_una_carrera_desde_running_serializa_y_no_pierde_escrituras(lanes) -> None:
    """Tres transiciones a la vez sobre una campaña abierta: el `flock` las ordena.

    ⚠️ Aquí NO se exige «gana una sola»: `running -> computed -> failed` son **dos** transiciones
    legítimas encadenadas, y exigir un único ganador era un error de mi primera versión de esta
    prueba, no del código. Lo que sí se exige es que cada éxito deje EXACTAMENTE una escritura
    (la `revision` cuenta los éxitos), que el resultado sea un estado alcanzable y que el archivo
    nunca quede a medias.
    """
    assert _abrir(lanes).returncode == 0
    ruta = str(lanes / "reports/campaign/campaign.json")
    base = [sys.executable, "-m", "tools.campaign_txn", "--path", ruta]
    ordenes = [
        [*base, "compute", "--input-gate", "passed", "--output-gate", "passed", "--consistency", "passed"],
        [*base, "fail", "--stage", "carrera", "--reason", "otro proceso"],
        [*base, "fail", "--stage", "carrera", "--reason", "un tercero"],
    ]
    codigos = _lanzar(ordenes, {**os.environ, "PYTHONPATH": str(RAIZ)})
    obj = _leer(ruta)
    exitos = codigos.count(0)
    assert exitos >= 1, f"alguna transición debía ganar: {codigos}"
    assert obj["revision"] == exitos, "la revisión cuenta los éxitos: ni se pierden ni se duplican"
    assert obj["status"] in {"computed", "failed"}
    assert cs.validate_schema(obj) == [], "una carrera no puede dejar el archivo a medias"


def test_una_carrera_no_puede_resucitar_un_estado_terminal(lanes) -> None:
    """Contra una campaña YA terminal, todas las transiciones simultáneas deben rebotar."""
    assert _abrir(lanes).returncode == 0
    assert _txn(lanes, "fail", "--stage", "previo", "--reason", "cerrada antes de la carrera").returncode == 0
    antes = _leer(lanes / "reports/campaign/campaign.json")
    ruta = str(lanes / "reports/campaign/campaign.json")
    base = [sys.executable, "-m", "tools.campaign_txn", "--path", ruta]
    ordenes = [
        [*base, "compute", "--input-gate", "passed", "--output-gate", "passed", "--consistency", "passed"],
        [*base, "fail", "--stage", "carrera", "--reason", "pisa al terminal"],
        [*base, "publish", "--release-sha", "c" * 40, "--manifest", str(_manifiesto(lanes))],
    ]
    codigos = _lanzar(ordenes, {**os.environ, "PYTHONPATH": str(RAIZ)})
    assert codigos.count(0) == 0, f"un terminal no retrocede: {codigos}"
    assert _leer(ruta) == antes, "el archivo no pudo cambiar ni un byte"


# =============================================================== 5 · prohibición de publicar
@pytest.mark.parametrize("preparar,estado", [(None, "ausente"), ("open", "running")])
def test_guard_bloquea_todo_estado_que_no_sea_validated(lanes, preparar, estado) -> None:
    if preparar == "open":
        assert _abrir(lanes).returncode == 0
    fin = _txn(lanes, "guard", "--manifest", str(_manifiesto(lanes)))
    assert fin.returncode == txn.EXIT_BLOCKED
    assert "BLOQUEADA" in fin.stderr


def test_guard_falla_cerrado_ante_una_transaccion_corrupta(lanes) -> None:
    """Un `campaign.json` manipulado o a medias NO puede leerse como permiso."""
    ruta = lanes / "reports/campaign/campaign.json"
    for contenido in ('{"status": "validated"}', "no es json", "", "[]"):
        ruta.write_text(contenido, encoding="utf-8")
        ok, motivo = txn.publishable(ruta, manifest=_manifiesto(lanes))
        assert not ok, f"{contenido!r} no puede autorizar: {motivo}"


def test_un_estado_validated_falsificado_a_mano_no_pasa_el_esquema(lanes) -> None:
    """Escribir 'validated' a mano no basta: el esquema exige recibo, revisor y marcas."""
    assert _abrir(lanes).returncode == 0
    ruta = lanes / "reports/campaign/campaign.json"
    obj = json.loads(ruta.read_text(encoding="utf-8"))
    obj["status"] = "validated"
    ruta.write_text(json.dumps(obj), encoding="utf-8")
    ok, motivo = txn.publishable(ruta, manifest=_manifiesto(lanes))
    assert not ok and "esquema" in motivo


def test_archivar_exige_que_la_campana_anterior_haya_terminado(lanes) -> None:
    """Relanzar sobre una campaña ABIERTA es el error que la máquina existe para impedir."""
    assert _abrir(lanes, "primera").returncode == 0
    fin = _txn(lanes, "archive", "--dir", str(lanes / "archivo"))
    assert fin.returncode == txn.EXIT_ERROR and "sigue ABIERTA" in fin.stderr
    assert _txn(lanes, "fail", "--stage", "x", "--reason", "y").returncode == 0
    assert _txn(lanes, "archive", "--dir", str(lanes / "archivo")).returncode == 0
    assert (lanes / "archivo" / "primera.json").is_file()
    assert not (lanes / "reports/campaign/campaign.json").exists()
    # …y ahora sí puede sellarse la siguiente, con identidad propia
    assert _abrir(lanes, "segunda").returncode == 0
    assert _leer(lanes / "reports/campaign/campaign.json")["campaign_id"] == "segunda"


def test_el_gestor_de_contexto_marca_failed_si_el_cuerpo_lanza(lanes) -> None:
    """La otra mitad del envoltorio: los runners en Python obtienen lo mismo sin repetir el trap."""
    ruta = lanes / "reports/campaign/campaign.json"
    with (
        pytest.raises(RuntimeError),
        txn.campaign(
            ruta,
            campaign_id="python",
            source_git_sha=SHA,
            git_dirty=False,
            panel=lanes / "panel.csv",
            input_seal_sha256=SELLO,
        ),
    ):
        raise RuntimeError("lane 3 reventó")
    obj = _leer(ruta)
    assert obj["status"] == "failed" and "lane 3 reventó" in obj["reason"]


# =============================================================== 6 · el cableado de los guiones
# ⚠️ Estas dos se comprueban leyendo el guion, NO ejecutándolo: correr el runbook real es una
# campaña de horas, y correr el publicador real publicaría. Se dice en vez de insinuar.
def test_el_runbook_canonico_conduce_la_transaccion() -> None:
    guion = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    assert "tools.campaign_txn" in guion, "el runbook canónico no conduce la transacción (#56)"
    assert "txn archive" in guion and "txn open" in guion
    assert "trap campaign_abort EXIT" in guion
    # `computed` sólo con las tres puertas en passed, y NUNCA antes del desenlace
    assert guion.index("txn open") < guion.index("txn compute")
    assert '--input-gate passed --output-gate passed --consistency "$CONSISTENCY_STATE"' in guion
    # ★ H2: la consistencia ya NO se cablea a `passed`; puede quedar pendiente sin ser terminal
    assert "CONSISTENCY_STATE=" in guion and "pending" in guion
    # Los desenlaces que SÍ son fallo registran su causa: la salida anormal (trap) y las etapas
    # obligatorias rotas. ⚠️ Son DOS, no tres: desde H2 la consistencia rota ya no manda a `failed`
    # —es el resultado esperado de una re-derivación— sino a `computed` con la consistencia
    # pendiente. Si algún día vuelven a ser tres, hay que mirar cuál se volvió terminal.
    # M74-E subió de 2 a 4 los `txn fail --if-open`, y cada uno existe por una razón distinta:
    # el trap de salida anormal, el de SIGINT, el de SIGTERM y el fail-fast de etapa obligatoria.
    # Un conteo desnudo sólo dice cuántos hay; esto dice POR QUÉ.
    assert guion.count("txn fail --if-open") == 4
    for razon in ("salida anormal del runbook", "detención autorizada", "etapa obligatoria fallida"):
        assert razon in guion, f"falta el registro de {razon!r}"
    assert 'txn fail --if-open --stage "consistencia"' not in guion


def _bloque_de_transaccion() -> str:
    """Extrae del runbook REAL sus líneas de transacción, para ejecutarlas tal cual."""
    guion = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    ini = guion.index("# ── Transacción de campaña")
    # ⚠️ El ancla es la ÚLTIMA línea del cableado (el trap de SIGHUP), no una intermedia. Anclar a
    # `trap 'exit 143' TERM` hizo que estas pruebas se rompieran en cuanto M74-E lo sustituyó por
    # `campaign_stop SIGTERM 143`, y peor: si el ancla intermedia hubiera sobrevivido, el ensayo
    # habría ejecutado un bloque TRUNCADO sin avisar. El final del bloque es el final del bloque.
    ancla = "trap 'exit 129' HUP"
    fin = guion.index(ancla) + len(ancla)
    return guion[ini:fin]


def test_el_cableado_real_del_runbook_se_ejecuta_y_registra_la_salida_anormal(tmp_path: Path) -> None:
    """CONDUCTUAL sobre el texto REAL: se extraen las líneas del runbook y se ejecutan.

    No es una paráfrasis del cableado: es el cableado, recortado del archivo que corre la campaña.
    Se le añade una salida en rojo y se exige que el `trap` la registre. La transacción vive en un
    temporal (`CAMPAIGN_TXN`), así que el repositorio no se toca: con `campaign.json` ausente,
    `archive` no crea ni escribe nada.
    """
    txn_path = tmp_path / "campaign.json"
    guion = tmp_path / "cableado.sh"
    guion.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        f'ANTE="{sys.executable}"\n'
        'CAMPAIGN_ID="extraida_0001"\n'
        f'CAMPAIGN_SHA="{SHA}"\n'
        'CAMPAIGN_DIRTY="false"\n'
        f'PREFLIGHT_SHA256="{SELLO}"\n'
        f'CAMPAIGN_TXN="{txn_path}"\n' + _bloque_de_transaccion() + '\necho "una etapa revienta"\nexit 5\n',
        encoding="utf-8",
    )
    fin = subprocess.run(
        ["bash", str(guion)],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(RAIZ)},
    )
    assert fin.returncode == 5, fin.stderr
    obj = cs.read(txn_path)
    assert obj is not None, f"el cableado real no selló la transacción: {fin.stderr}"
    assert obj["status"] == "failed" and obj["exit_code"] == 5
    assert obj["campaign_id"] == "extraida_0001" and obj["source_git_sha"] == SHA
    # la huella del panel se DERIVA del archivo real que declara el runbook
    assert obj["panel_sha256"] == txn.panel_fingerprint(RAIZ / "data/processed/visa_panel_long.csv")
    # y el repositorio no quedó tocado
    assert not (RAIZ / "reports/campaign/campaign.json").exists()


def test_el_publicador_exige_la_transaccion_antes_de_cada_publicacion() -> None:
    guion = (RAIZ / "experiments" / "sync_all.sh").read_text(encoding="utf-8")
    assert "txn_guard" in guion, "el publicador no consulta la transacción (#56)"
    # toda orden que publica va DESPUÉS de una consulta a la transacción
    ultima_guarda = -1
    ordenes_vistas = 0
    for numero, linea in enumerate(guion.splitlines()):
        codigo = linea.split("#", 1)[0]  # un comentario que NOMBRA `git push` no publica nada
        if "txn_guard ||" in codigo:
            ultima_guarda = numero
        if any(o in codigo for o in ("dvc push", "git push")):
            ordenes_vistas += 1
            assert 0 <= ultima_guarda < numero, f"línea {numero + 1} publica sin guarda previa: {linea.strip()!r}"
    assert ordenes_vistas >= 2, "el barrido no encontró las órdenes que publican: la prueba sería vacía"

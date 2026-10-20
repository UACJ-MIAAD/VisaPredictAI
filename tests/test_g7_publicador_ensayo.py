"""G7 · desbloqueo (fase 3) — ensayo COMPLETO del publicador, con remotos de prueba y las órdenes reales.

Un candidato con punteros DVC se valida con un `/3`; su PR se «mergea» (squash con EXACTAMENTE su árbol) en un remoto git
bare; un checkout de la release corre `experiments/sync_all.sh --publish` REAL, que llama a `campaign_txn publish-release`
REAL con el DVC REAL contra un remoto DVC local. Nada toca remotos de producción.

Lo que se exige: sólo sale el árbol aprobado; las comprobaciones (puerta, árbol, limpieza, integración) ocurren ANTES
del primer efecto externo; un fallo de `dvc push` no consume el permiso y el reintento es seguro; publicar dos veces no
está autorizado; y la sección de publicación se serializa frente a una revalidación.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.conftest import requiere
from tests.test_g7_publicacion_reconciliada import (  # noqa: F401 — fixtures y ayudantes
    _commit,
    _git,
    _recibo3,
    _validar_cli,
    candidato,
    cerrada,
)
from tests.test_g7_reconciliacion import (  # noqa: F401 — fixtures que encadena `cerrada`
    _cerrado,
    _codigo_del_checkout,
    _ensamblar,
    _resellar_plan,
    c5,
    cab,
    con_e3,
    cx,
    m5,
    mundo,
    rc,
    rm,
    score,
)
from tools import campaign_state as cs
from tools import campaign_txn as txn

RAIZ = Path(__file__).resolve().parents[1]


def _dvc() -> Path:
    requiere(modulos=("dvc",))  # el DVC real: lo instala el job `model-tests` (y lo trae el entorno gobernado local)
    junto = Path(sys.executable).parent / "dvc"
    encontrado = junto if junto.is_file() else Path(shutil.which("dvc") or "")
    if not encontrado.is_file():
        pytest.fail("DVC importable pero sin ejecutable `dvc` junto al intérprete ni en el PATH", pytrace=False)
    return encontrado


def _dvc_en(dvc: Path, repo: Path, *args: str) -> None:
    fin = subprocess.run([str(dvc), *args], cwd=repo, capture_output=True, text=True,
                         env={**os.environ, "DVC_NO_ANALYTICS": "1"})  # fmt: skip
    assert fin.returncode == 0, (args, fin.stdout, fin.stderr)


@pytest.fixture
def mundo_publicable(candidato: dict, tmp_path: Path, monkeypatch, capsys) -> dict:  # noqa: F811 — fixture importada
    """Candidato con DVC y el `sync_all.sh` real, validado con un /3, «mergeado» por squash en `origin/main`."""
    dvc = _dvc()
    m = dict(candidato)
    cand = m["cand"]
    remoto_dvc = tmp_path / "dvc_remoto"
    remoto_dvc.mkdir()
    (cand / ".gitignore").write_text("/ante/\n/ante_nf/\n", encoding="utf-8")
    _dvc_en(dvc, cand, "init", "-q")
    _dvc_en(dvc, cand, "remote", "add", "-d", "ensayo", str(remoto_dvc))
    (cand / "models").mkdir()
    (cand / "models" / "modelo.bin").write_bytes(b"pesos del modelo\n")
    _dvc_en(dvc, cand, "add", "models")
    (cand / "experiments").mkdir()
    shutil.copy2(RAIZ / "experiments" / "sync_all.sh", cand / "experiments" / "sync_all.sh")
    shutil.copy2(RAIZ / "experiments/score_e3_campaign.py", cand / "experiments/score_e3_campaign.py")
    (cand / "tools").mkdir()
    shutil.copy2(RAIZ / "tools/publication_runtime.py", cand / "tools/publication_runtime.py")
    base = _git(cand, "rev-parse", "HEAD")
    aprobado = _commit(cand, "candidato con punteros DVC y el publicador")
    m["arbol"] = _git(cand, "rev-parse", "HEAD^{tree}")
    rc_, err = _validar_cli(m, _recibo3(m, tmp_path / "recibo3.json"), monkeypatch, capsys)
    assert rc_ == 0, err
    # el «merge» de la PR: un squash con EXACTAMENTE el árbol aprobado, sobre la base, en el `main` de un remoto bare
    origen = tmp_path / "origen.git"
    # la rama inicial EXPLÍCITA: sin `init.defaultBranch` (el runner) el HEAD del bare apunta a `master`, que no existe
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origen)], check=True)
    _git(cand, "push", "-q", str(origen), f"{base}:refs/heads/main")
    squash = _git(cand, "commit-tree", m["arbol"], "-p", base, "-m", "G7 (#99)")
    _git(cand, "push", "-q", str(origen), f"{squash}:refs/heads/main")
    release = tmp_path / "release"
    subprocess.run(["git", "clone", "-q", str(origen), str(release)], check=True)
    # la caché DVC de quien preparó el corte (en producción, la máquina que lo calculó); local, no versionada
    (release / ".dvc" / "config.local").write_text(f"[cache]\n    dir = {cand / '.dvc' / 'cache'}\n", encoding="utf-8")
    entorno = {
        **os.environ,
        "CAMPAIGN_TXN": str(m["txn"]),
        "CAMPAIGN_MANIFEST": str(m["txn"].with_name("campaign_manifest.json")),
        "VP_DVC": str(dvc),
        "VP_PUBLISH_PYTHON": sys.executable,
        "PYTHONPATH": str(RAIZ),
        "DVC_NO_ANALYTICS": "1",
        "SYNC_PUBLISH": "0",
    }
    return {**m, "dvc": dvc, "remoto_dvc": remoto_dvc, "origen": origen, "release": release, "squash": squash,
            "aprobado": aprobado, "entorno": entorno}  # fmt: skip


def _publicar(m: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "experiments/sync_all.sh", "--publish"], cwd=m["release"], env=m["entorno"],
                          capture_output=True, text=True, timeout=600)  # fmt: skip


def _objetos(remoto: Path) -> list[Path]:
    return sorted(p for p in remoto.rglob("*") if p.is_file())


def _estado(m: dict) -> dict:
    return cs.read(m["txn"]) or {}


# ═══════════════════════════════ el camino completo
def test_ensayo_completo_publica_el_arbol_aprobado_y_nada_mas(mundo_publicable: dict) -> None:
    m = mundo_publicable
    main_antes = _git(m["origen"], "rev-parse", "main")
    fin = _publicar(m)
    assert fin.returncode == 0, fin.stdout + fin.stderr
    estado = _estado(m)
    assert estado["status"] == "published" and estado["release_sha"] == m["squash"]
    assert _git(m["origen"], "rev-parse", "main") == main_antes, "publicar no empuja git"
    assert _git(m["release"], "rev-parse", "HEAD") == m["squash"]
    assert _git(m["release"], "rev-parse", "HEAD^{tree}") == m["arbol"], "lo publicado es el árbol aprobado"
    assert _objetos(m["remoto_dvc"]), "dvc push llevó los objetos de los punteros al remoto"
    # repetir no está autorizado: el permiso se consumió
    otra = _publicar(m)
    assert otra.returncode == 8 and "published" in otra.stderr + otra.stdout, otra.stdout + otra.stderr


def test_sin_cambios_de_punteros_se_publica_igual_sin_commit_inutil(mundo_publicable: dict) -> None:
    """Los objetos ya están en el remoto (corte con los mismos punteros): `dvc push` no tiene nada que subir y la
    release se publica igual; ni se fuerza un commit ni se devuelve verde sin consumir el permiso."""
    m = mundo_publicable
    _dvc_en(m["dvc"], m["cand"], "push")
    antes = _objetos(m["remoto_dvc"])
    fin = _publicar(m)
    assert fin.returncode == 0, fin.stdout + fin.stderr
    assert _objetos(m["remoto_dvc"]) == antes and _estado(m)["status"] == "published"


# ═══════════════════════════════ ninguna comprobación ocurre después del primer efecto externo
def test_un_head_con_otro_arbol_no_publica_ni_empuja_nada(mundo_publicable: dict) -> None:
    m = mundo_publicable
    (m["release"] / "extra.txt").write_text("no revisado\n", encoding="utf-8")
    _commit(m["release"], "cambio tras la firma")
    fin = _publicar(m)
    assert fin.returncode == 9 and "árbol revisado" in fin.stderr, fin.stdout + fin.stderr
    assert _objetos(m["remoto_dvc"]) == [] and _estado(m)["status"] == "validated"


def test_una_release_no_integrada_en_main_no_publica(mundo_publicable: dict) -> None:
    m = mundo_publicable
    _git(m["origen"], "update-ref", "refs/heads/main", _git(m["origen"], "rev-parse", "main~1"))
    _git(m["release"], "fetch", "-q", "--force", "origin", "+refs/heads/main:refs/remotes/origin/main")
    fin = _publicar(m)
    assert fin.returncode == 9 and "no está integrado" in fin.stderr, fin.stdout + fin.stderr
    assert _objetos(m["remoto_dvc"]) == [] and _estado(m)["status"] == "validated"


def test_un_checkout_sucio_no_publica(mundo_publicable: dict) -> None:
    m = mundo_publicable
    (m["release"] / "suelto.txt").write_text("sin commitear\n", encoding="utf-8")
    fin = _publicar(m)
    assert fin.returncode == 9 and "limpio" in fin.stderr, fin.stdout + fin.stderr
    assert _objetos(m["remoto_dvc"]) == [] and _estado(m)["status"] == "validated"


def test_un_dvc_push_fallido_no_consume_el_permiso_y_el_reintento_publica(mundo_publicable: dict) -> None:
    m = mundo_publicable
    caido = m["release"] / ".dvc" / "config.local"
    original = caido.read_text(encoding="utf-8")
    caido.write_text(original + "['remote \"ensayo\"']\n    url = /dev/null/remoto_caido\n", encoding="utf-8")
    fin = _publicar(m)
    assert fin.returncode == 9 and "dvc push falló" in fin.stderr, fin.stdout + fin.stderr
    assert _estado(m)["status"] == "validated", "un efecto fallido no consume el permiso"
    caido.write_text(original, encoding="utf-8")
    otra = _publicar(m)
    assert otra.returncode == 0 and _estado(m)["status"] == "published", otra.stdout + otra.stderr


def test_un_permiso_sustituido_no_publica_el_arbol_viejo(mundo_publicable: dict, tmp_path, monkeypatch, capsys) -> None:
    """Tras revalidar a otro candidato, la release del árbol viejo (aunque esté en main) ya no se publica."""
    import datetime as dt

    m = mundo_publicable
    (m["cand"] / "nuevo.txt").write_text("candidato 2\n", encoding="utf-8")
    _commit(m["cand"], "candidato 2")
    vigente = _estado(m)["validated_at"]
    despues = dt.datetime.fromisoformat(vigente.replace("Z", "+00:00")) + dt.timedelta(minutes=1)
    recibo2 = _recibo3(m, tmp_path / "recibo3_nuevo.json", reviewed_at=despues.strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr(txn, "_run_consistency", lambda: (True, ""))
    assert txn.main(["--path", str(m["txn"]), "revalidate", "--receipt", str(recibo2)]) == 0, capsys.readouterr().err
    fin = _publicar(m)
    assert fin.returncode in (8, 9), fin.stdout + fin.stderr
    assert _objetos(m["remoto_dvc"]) == [] and _estado(m)["status"] == "validated"


# ═══════════════════════════════ la sección de publicación se serializa frente a una revalidación
def test_una_revalidacion_espera_a_que_termine_la_publicacion(mundo_publicable: dict) -> None:
    m = mundo_publicable
    dentro, soltar = threading.Event(), threading.Event()
    salida: dict[str, object] = {}

    def empujar_lento(repo: Path) -> None:
        dentro.set()
        assert soltar.wait(60)
        txn.empujar_dvc(repo)

    def publicar() -> None:
        try:
            salida["publicar"] = txn.publicar_release(m["txn"], manifest=m["txn"].with_name("campaign_manifest.json"),
                                                      repo=m["release"], empujar=empujar_lento)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 — se informa en la aserción
            salida["publicar"] = exc

    def revalidar() -> None:
        estado = m["txn"].read_bytes()
        obj = cs.loads(estado.decode("utf-8"))
        try:
            salida["revalidar"] = cs.revalidate(
                m["txn"], validation_receipt_sha256="e" * 64, validation_receipt_path=str(m["txn"].parent / "otro.json"),
                reviewed_by="Javier Rebull", decision="aprobada", validated_at=txn.now_rfc3339(),
                expected_revision=obj["revision"], expected_state_sha256=__import__("hashlib").sha256(estado).hexdigest(),
            )  # fmt: skip
        except ValueError as exc:
            salida["revalidar"] = exc

    monkeypatch_env = {k: m["entorno"][k] for k in ("VP_DVC", "DVC_NO_ANALYTICS")}
    os.environ.update(monkeypatch_env)
    try:
        hilo_pub = threading.Thread(target=publicar)
        hilo_pub.start()
        assert dentro.wait(120), "la publicación no llegó al efecto externo"
        hilo_rev = threading.Thread(target=revalidar)
        hilo_rev.start()
        hilo_rev.join(2)
        assert hilo_rev.is_alive(), "la revalidación no esperó al lock de la publicación"
        soltar.set()
        hilo_pub.join(300)
        hilo_rev.join(60)
    finally:
        for k in monkeypatch_env:
            os.environ.pop(k, None)
    assert isinstance(salida["publicar"], dict) and salida["publicar"]["status"] == "published", salida
    assert isinstance(salida["revalidar"], ValueError) and "published" in str(salida["revalidar"]), salida

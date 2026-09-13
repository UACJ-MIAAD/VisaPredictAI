"""Recibo cerrado de la configuración ganadora del HPO profundo (enmienda §8.6, M74-E).

La campaña busca **una vez** (`_cfg_bitcn`, 40 trials, semilla 1) y sella la ganadora en
``reports/campaign/hpo_deep_best_{table}_{model}.json``. Todo lo que venga después —los reentrenos
multi-semilla y la finalización— **consume** esa ganadora; nadie vuelve a optimizar.

El defecto que motiva este módulo: ``save_finalists_deep.py`` abría una **segunda** búsqueda Optuna
de 15 trials con un espacio distinto, así que el «finalista AutoBiTCN» podía no ser el modelo que
la campaña había evaluado. Y el consumidor no tenía forma de notarlo: un JSON de ganadora es un
diccionario plano, sin identidad, que se lee igual venga de esta campaña o de la de agosto.

**Por qué el recibo vive aquí y es stdlib puro:** sus dos consumidores corren en intérpretes
distintos —``run_global_deep`` y ``save_finalists_deep`` en ``ante_nf``, sin ``vp_data``— así que
no puede depender del paquete del producto. Se importa como ``from hpo_winner_receipt import …``,
el mismo mecanismo con el que ``save_finalists_deep`` ya importa ``run_global_deep``.

⚠️ **La acreditación vive en el LECTOR.** Escribir el recibo no protege nada por sí solo: quien
decide entrenar es quien tiene que volver a comprobarlo. Es la misma lección que M73 aprendió con
las invariantes de estado y M74-B-R2 con el recibo de validación humana.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: Esquema **cerrado**: ni claves de menos ni de más.
RECEIPT_SCHEMA = "hpo-winner-receipt/1"
RECEIPT_KEYS = frozenset(
    {
        "schema",
        "campaign_id",
        "code_sha",
        "panel_sha256",
        "table",
        "model",
        "search_space",
        "n_trials",
        "search_seed",
        "winner_sha256",
        "accelerator_in_artifact",
    }
)
#: El procedimiento declarado en §8.6.1. Una ganadora que no lo acredite no es la de esta campaña.
SEARCH_SPACE = "_cfg_bitcn"
N_TRIALS = 40
SEARCH_SEED = 1
#: §8.6.4 · las tres acreditaciones del acelerador exigen este valor en las tres.
ACCELERATOR = "cpu"
#: Claves del JSON ganador que NO son kwargs del modelo (misma lista que run_global_deep).
CONFIG_DROP = frozenset({"h", "loss", "valid_loss", "pool_key", "freq_key"})


class WinnerReceiptError(ValueError):
    """La ganadora sellada no acredita pertenecer a ESTA campaña, o su recibo no la describe."""


PANEL_REL = Path("data") / "processed" / "visa_panel_long.csv"


def panel_fingerprint(path: str | Path) -> str:
    """Huella del panel en la MISMA forma que sella la transacción (``sha256:`` + 64 hex)."""
    return "sha256:" + sha256_file(path)


def _head_sha(root: str | Path) -> str:
    """HEAD vivo del árbol. Fail-closed: sin git no se puede acreditar el código."""
    import subprocess

    try:
        fin = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=30, check=False
        )
    except OSError as exc:
        raise WinnerReceiptError(f"no se pudo leer HEAD: {exc}") from exc
    if fin.returncode != 0:
        raise WinnerReceiptError(f"no se pudo leer HEAD: {fin.stderr.strip()[:120]}")
    return fin.stdout.strip()


def campaign_identity(root: str | Path) -> dict[str, str]:
    """Identidad de la campaña **en curso**, leída de su transacción. Fail-closed.

    ⚠️ Exige ``status == "running"`` **exactamente**. Un `computed`, `validated`, `failed` o
    `published` no es una campaña en curso: sellar o consumir una ganadora bajo un estado terminal
    describiría una corrida que ya terminó.

    ⚠️ Se usa ESTE ``panel_sha256`` y no el ``panel_hash`` de ``save_finalists_deep._identity()``,
    que es un **md5 truncado a 12** pese a lo que sugeriría el nombre del campo. Ligar el recibo a
    un md5 llamado ``panel_sha256`` habría metido una mentira de nombre en el artefacto que existe
    para impedir mentiras de identidad.
    """
    ruta = Path(root) / "reports" / "campaign" / "campaign.json"
    if not ruta.is_file():
        raise WinnerReceiptError(
            f"no hay transacción de campaña en {ruta}: sin ella no puede acreditarse a qué corrida "
            "pertenece la ganadora"
        )
    try:
        estado = _loads_sin_duplicados(ruta.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise WinnerReceiptError(f"transacción de campaña ilegible: {exc}") from exc
    if estado.get("status") != "running":
        raise WinnerReceiptError(
            f"la transacción está en {estado.get('status')!r} y no en 'running': no hay campaña en curso"
        )
    for clave in ("campaign_id", "source_git_sha", "panel_sha256"):
        if not isinstance(estado.get(clave), str) or not estado[clave]:
            raise WinnerReceiptError(f"la transacción no sella un {clave}")
    return {k: str(estado[k]) for k in ("campaign_id", "source_git_sha", "panel_sha256")}


def sealed_panel_sha256(root: str | Path) -> str:
    """Compatibilidad: el panel sellado por la transacción en curso."""
    return campaign_identity(root)["panel_sha256"]


def receipt_path(winner: str | Path) -> Path:
    """El recibo vive junto a su ganadora, con el mismo nombre y sufijo ``.receipt.json``."""
    p = Path(winner)
    return p.with_suffix(".receipt.json")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _loads_sin_duplicados(texto: str) -> dict:
    """``json.loads`` que RECHAZA claves duplicadas: nadie esconde un segundo ``campaign_id``."""

    def _no_dupes(pares: list[tuple[str, object]]) -> dict:
        visto: dict[str, object] = {}
        for k, v in pares:
            if k in visto:
                raise ValueError(f"clave JSON duplicada: {k!r}")
            visto[k] = v
        return visto

    obj = json.loads(texto, object_pairs_hook=_no_dupes)
    if not isinstance(obj, dict):
        raise ValueError("no es un objeto JSON")
    return obj


def write_receipt(
    winner: str | Path,
    *,
    campaign_id: str,
    code_sha: str,
    panel_sha256: str,
    table: str,
    model: str,
    accelerator_in_artifact: str,
) -> Path:
    """Sella el recibo de una ganadora recién escrita. Lo llama QUIEN BUSCA, una sola vez."""
    ruta = Path(winner)
    acta = {
        "schema": RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "code_sha": code_sha,
        "panel_sha256": panel_sha256,
        "table": table,
        "model": model,
        "search_space": SEARCH_SPACE,
        "n_trials": N_TRIALS,
        "search_seed": SEARCH_SEED,
        "winner_sha256": sha256_file(ruta),
        "accelerator_in_artifact": accelerator_in_artifact,
    }
    destino = receipt_path(ruta)
    # staging + promoción atómica: una caída a mitad no deja un recibo truncado que luego parezca
    # corrupto en vez de ausente. El hash ya lo dejaría fail-closed, pero un archivo a medias
    # confunde el diagnóstico.
    tmp = destino.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(acta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, destino)
    return destino


def effective_accelerator() -> str:
    """El acelerador que el ENTORNO oficial impone (§8, ``VP_DEEP_ACCEL``). Sin adivinar nada.

    ⚠️ No llama a ``run_global_deep._accelerator()``, que **prefiere MPS si existe** y sólo cae a
    CPU por ausencia de hardware. Aquí la ausencia de la variable es un **fallo**, no un valor por
    defecto: una campaña oficial declara su acelerador, no lo hereda de la máquina.
    """
    return os.environ.get("VP_DEEP_ACCEL", "")


def build_finalist(cls: Any, cfg: Mapping[str, Any], *, seed: int = SEARCH_SEED) -> Any:
    """Instancia el finalista desde una configuración **ya acreditada**, fijando CPU.

    El acelerador se escribe aquí, después de desempaquetar ``cfg``, y **no se vuelve a consultar
    `_accelerator()`**: lo que llega al constructor es el valor del protocolo, no el de la máquina.
    La clase entra por parámetro para que una prueba pueda **espiar qué recibe de verdad** sin
    necesitar ``neuralforecast``.
    """
    params = {
        **{k: v for k, v in cfg.items() if k not in CONFIG_DROP},
        "h": 1,
        "random_seed": seed,
        "enable_progress_bar": False,
        "enable_model_summary": False,
        "logger": False,
        "accelerator": ACCELERATOR,
    }
    return cls(**params)


def load_and_accredit(
    winner: str | Path,
    *,
    root: str | Path,
    table: str,
    model: str,
) -> dict[str, Any]:
    """Acredita la ganadora contra ESTA campaña y devuelve su configuración. Fail-closed.

    ⚠️ **El llamador no declara la identidad: la lee este acreditador.** Una versión anterior
    recibía ``campaign_id``/``code_sha``/``panel_sha256`` por argumento, y el finalizador los
    tomaba del entorno — de modo que quien quisiera saltarse la comprobación sólo tenía que
    exportar las variables correctas. La identidad sale ahora de la transacción sellada, del panel
    en disco y de HEAD.

    Comprueba **igualdad triple**, no dos declaraciones entre sí:

    ``receipt.panel_sha256 == transacción.panel_sha256 == sha256(panel actual)``
    ``receipt.code_sha == transacción.source_git_sha == HEAD actual``  (40 chars, sin truncar)

    Dos declaraciones coherentes entre sí pueden estar ambas equivocadas respecto del mundo: el
    tercer término es el que las ancla a lo que hay en el disco.

    Lanza :class:`WinnerReceiptError` nombrando exactamente qué no cuadra. **Se llama ANTES de
    construir el modelo.**
    """
    raiz = Path(root)
    ident = campaign_identity(raiz)  # exige status == running
    campaign_id = ident["campaign_id"]
    #: ⚠️ SHA **completo**, no `[:7]`. Dos corridas del mismo día pueden compartir prefijo, así que
    #: truncar convierte una identidad en un parecido. El SHA corto sobrevive SÓLO en el manifiesto
    #: histórico, que ya tenía esa convención y cuyo gate la exige.
    code_sha = ident["source_git_sha"]
    panel_sha256 = ident["panel_sha256"]

    # ── tercer término del panel: lo que hay AHORA en disco
    panel = raiz / PANEL_REL
    if not panel.is_file():
        raise WinnerReceiptError(f"no existe el panel {panel}: no puede cerrarse la igualdad triple")
    vivo = panel_fingerprint(panel)
    if vivo != panel_sha256:
        raise WinnerReceiptError(
            f"el panel en disco ({vivo[:19]}…) no es el que selló la transacción "
            f"({panel_sha256[:19]}…): el panel cambió bajo la campaña"
        )

    # ── tercer término del código: HEAD vivo
    head = _head_sha(raiz)
    if head != ident["source_git_sha"]:
        raise WinnerReceiptError(
            f"HEAD es {head[:7]} y la transacción selló {ident['source_git_sha'][:7]}: "
            "el árbol se movió durante la campaña"
        )
    ruta = Path(winner)
    if not ruta.is_file():
        raise WinnerReceiptError(f"no existe la configuración ganadora {ruta}")
    acta_path = receipt_path(ruta)
    if not acta_path.is_file():
        raise WinnerReceiptError(
            f"la ganadora {ruta.name} no lleva recibo ({acta_path.name}): no puede acreditarse que "
            "proceda de esta campaña"
        )
    try:
        acta = _loads_sin_duplicados(acta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise WinnerReceiptError(f"recibo ilegible o con claves duplicadas: {exc}") from exc

    faltan = sorted(RECEIPT_KEYS - set(acta))
    sobran = sorted(set(acta) - RECEIPT_KEYS)
    if faltan or sobran:
        raise WinnerReceiptError(f"esquema del recibo: faltan {faltan}, sobran {sobran}")
    if acta["schema"] != RECEIPT_SCHEMA:
        raise WinnerReceiptError(f"esquema {acta['schema']!r}; se esperaba {RECEIPT_SCHEMA!r}")

    # ── el contenido no ha cambiado desde que se selló
    real = sha256_file(ruta)
    if acta["winner_sha256"] != real:
        raise WinnerReceiptError(
            f"{ruta.name} cambió desde el sellado (sha {real[:12]}… ≠ {str(acta['winner_sha256'])[:12]}…)"
        )

    # ── pertenece a ESTA campaña, no a otra ni a otra añada
    for clave, esperado in (
        ("campaign_id", campaign_id),
        ("code_sha", code_sha),
        ("panel_sha256", panel_sha256),
        ("table", table),
        ("model", model),
    ):
        if acta[clave] != esperado:
            raise WinnerReceiptError(
                f"el recibo dice {clave}={acta[clave]!r} y la campaña activa tiene {esperado!r}: "
                "la ganadora procede de otra corrida"
            )

    # ── describe el procedimiento declarado en §8.6.1, no otro
    #: nombre propio: `esperado` ya quedó ligado a `str` en el bucle de identidad y aquí los
    #: valores son enteros; reutilizarlo hacía fallar el gate de tipos.
    procedimiento: tuple[tuple[str, object], ...] = (
        ("search_space", SEARCH_SPACE),
        ("n_trials", N_TRIALS),
        ("search_seed", SEARCH_SEED),
    )
    for clave, declarado in procedimiento:
        if acta[clave] != declarado:
            raise WinnerReceiptError(
                f"el recibo declara {clave}={acta[clave]!r} y el protocolo congelado exige {declarado!r}"
            )

    # ── §8.6.4 · las TRES acreditaciones del acelerador, por separado
    cfg = _loads_sin_duplicados(ruta.read_text(encoding="utf-8"))
    en_artefacto = cfg.get("accelerator")
    if acta["accelerator_in_artifact"] != ACCELERATOR or en_artefacto != ACCELERATOR:
        raise WinnerReceiptError(
            f"la ganadora declara accelerator={en_artefacto!r} (recibo: "
            f"{acta['accelerator_in_artifact']!r}) y esta campaña es {ACCELERATOR!r}. El valor "
            "efectivo la sobrescribiría, pero una ganadora de otro entorno falsearía la procedencia"
        )
    efectivo = effective_accelerator()
    if efectivo != ACCELERATOR:
        raise WinnerReceiptError(
            f"VP_DEEP_ACCEL={efectivo!r} y el protocolo exige {ACCELERATOR!r}: el entorno oficial "
            "de la campaña no está fijado (§8)"
        )
    return cfg

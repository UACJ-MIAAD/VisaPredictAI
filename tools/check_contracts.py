"""Contratos cross-repo (B3, plan auditoría 2026-07-11).

Valida cada artefacto publicado contra su contrato versionado en
``vp_data/contracts/*.json`` (columnas requeridas para CSV; llaves y tipos top-level
para JSON) y exige que TODO el corte comparta la misma añada: los artefactos que
declaran ``vintage_key`` deben coincidir entre sí y con la añada real del panel
(``max(bulletin_date)``) — un corte con añadas mezcladas FALLA.

Cero dependencias (ni pandas): corre igual en el CI dev, en el cron (antes del
manifiesto de release) y en un clone pelón. El lado TypeScript vendoriza estos mismos
contratos (``lib/contracts/`` del repo web) y los verifica al construir; el manifiesto
de release los lista como artefactos required, así el loader detecta la deriva
vendored-vs-publicado por hash.

Corre desde la raíz:  python tools/check_contracts.py
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = ROOT / "vp_data" / "contracts"

TYPES: dict[str, type | tuple] = {
    "str": str,
    "int": int,
    "float": float,
    "number": (int, float),
    "dict": dict,
    "list": list,
    "bool": bool,
}


def _panel_vintage(root: Path) -> str | None:
    """``max(bulletin_date)[:7]`` leyendo el CSV a mano (sin pandas)."""
    panel = root / "data" / "processed" / "visa_panel_long.csv"
    if not panel.exists():
        return None
    with panel.open() as fh:
        header = fh.readline().strip().split(",")
        if "bulletin_date" not in header:
            return None
        idx = header.index("bulletin_date")
        best = ""
        for line in fh:
            cols = line.rstrip("\n").split(",")
            if len(cols) > idx and cols[idx] > best:
                best = cols[idx]
    return best[:7] or None


def check(root: Path = ROOT, contracts_dir: Path = CONTRACTS_DIR) -> list[str]:
    problems: list[str] = []
    vintages: dict[str, str] = {}
    contracts = sorted(contracts_dir.glob("*.json"))
    if not contracts:
        return [f"sin contratos en {contracts_dir}"]
    for cpath in contracts:
        c = json.loads(cpath.read_text())
        art = root / c["artifact"]
        if not art.exists():
            problems.append(f"{c['artifact']}: artefacto ausente")
            continue
        if c["kind"] == "csv":
            # D2-C: `art.open()` sin cerrar dejaba el descriptor a merced del GC y emitía
            # ResourceWarning; con `error` global en la suite eso es un fallo, y con razón.
            with art.open() as fh:
                header = fh.readline().strip().split(",")
            missing = [col for col in c["required_columns"] if col not in header]
            if missing:
                problems.append(f"{c['artifact']}: columnas requeridas ausentes {missing}")
        else:
            try:
                data = json.loads(art.read_text())
            except json.JSONDecodeError as e:
                problems.append(f"{c['artifact']}: JSON ilegible ({e})")
                continue
            for key, tname in c.get("required_keys", {}).items():
                if key not in data:
                    problems.append(f"{c['artifact']}: llave requerida ausente '{key}'")
                elif not isinstance(data[key], TYPES[tname]):
                    problems.append(f"{c['artifact']}: '{key}' debería ser {tname}, es {type(data[key]).__name__}")
            # R0-03 (reauditoría ciega): un contrato que solo exige dicts top-level es
            # nominal — la deriva de esquema que motivó A-04 (gate_scope/holdout_winner
            # ausentes) pasaba limpia. required_paths exige rutas anidadas con tipo.
            for dotted, tname in c.get("required_paths", {}).items():
                node = data
                for part in dotted.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                    if node is None:
                        problems.append(f"{c['artifact']}: ruta requerida ausente '{dotted}'")
                        break
                else:
                    if not isinstance(node, TYPES[tname]):
                        problems.append(f"{c['artifact']}: '{dotted}' debería ser {tname}, es {type(node).__name__}")
            vk = c.get("vintage_key")
            if vk and isinstance(data.get(vk), str):
                vintages[c["artifact"]] = str(data[vk])[:7]
    pv = _panel_vintage(root)
    if pv:
        vintages["data/processed/visa_panel_long.csv (real)"] = pv
    if len(set(vintages.values())) > 1:
        problems.append(f"CORTE CON AÑADAS MEZCLADAS: {vintages}")
    # Auditoría 11-jul (+rondas 2-3, 12-jul): la identidad del manifiesto PUBLICADO debe
    # RESOLVER a un commit. Se exige: (a) forma 12-hex (ni 'n/d' ni sufijo -dirty), y
    # (b) que el objeto exista de verdad (`git cat-file -e`). FAIL-CLOSED total (ronda 3:
    # el bypass shallow dejaba pasar un sha fantasma justo en CI/cron): un clone shallow
    # o sin git ES violación — los checkouts que corren este gate usan fetch-depth: 0.
    man = root / "reports" / "release" / "release_manifest.json"
    if man.exists():
        try:
            manifest = json.loads(man.read_text())
            sha = str(manifest.get("git_sha", ""))
            # Reauditoría 12-jul (hueco cazado en vivo): cambiar un artefacto listado SIN
            # regenerar el manifiesto solo lo detectaba el verifyEntry del web (stale en
            # el deploy). El manifiesto debe describir los bytes del árbol AHORA.

            for a in manifest.get("artifacts", []):
                ap = root / a["path"]
                # Reauditoría 2 (12-jul): `if ap.exists() and…` era fail-open — un artefacto
                # required listado pero BORRADO producía cero problemas.
                if not ap.exists():
                    problems.append(
                        f"release_manifest.json: '{a['path']}' listado en el manifiesto pero AUSENTE del árbol"
                    )
                elif hashlib.sha256(ap.read_bytes()).hexdigest() != a["sha256"]:
                    problems.append(
                        f"release_manifest.json: '{a['path']}' cambió tras sellar el manifiesto — regenerarlo"
                    )
            if sha.endswith("-dirty"):
                problems.append(f"release_manifest.json: git_sha '{sha}' es -dirty — regenerar con árbol limpio")
            elif not re.fullmatch(r"[0-9a-f]{12}", sha):
                problems.append(f"release_manifest.json: git_sha '{sha}' no es un sha 12-hex resoluble")
            else:
                problems.extend(_sha_unresolvable(root, sha))
        except json.JSONDecodeError:
            problems.append("release_manifest.json: JSON ilegible")
    # D4: la identidad del corte (tarjeta ⇄ manifiesto) se verifica en el mismo gate.
    problems.extend(release_identity_problems(root))
    return problems


# --------------------------------------------------------------------------------------
# D4: identidad del release, sin ciclo tarjeta↔manifiesto
# --------------------------------------------------------------------------------------
#: Campo estructural que lleva el id en la tarjeta. DEBE coincidir con
#: ``build_model_card.RELEASE_ID_FIELD`` (un test lo fija); aquí se replica porque este
#: verificador es stdlib puro y corre en el job `consistency`, que no instala el extra `model`.
RELEASE_ID_FIELD = "release_id: "
RELEASE_ID_RE = re.compile(rf"(?m)^{re.escape(RELEASE_ID_FIELD)}(\S+)$")
IDENTITY_SCHEMA = 2
IDENTITY_KEYS = frozenset({"schema", "card_path", "sentinel", "digest", "card_sha256_normalized"})
CARD_PATH = "reports/governance/MODEL_CARD.md"
MANIFEST_PATH = "reports/release/release_manifest.json"
RELEASE_ID_SENTINEL = "@@RELEASE_ID_SENTINEL@@"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: PUENTE CERRADO. El ÚNICO corte anterior a D4 que se acepta sin bloque `identity` es este,
#: acreditado por sus cuatro valores exactos. Cualquier otro manifiesto sin `identity` falla:
#: el puente no es una categoría, es una excepción nominal e irrepetible.
LEGACY_MANIFEST_SHA256 = "8c362dbbacc245fdf4ba4b9835af096f81257ebc8f0a03c12a065484fbed3145"
LEGACY_CARD_SHA256 = "3cc175f4641547f7e19998495ed6d6422db53bb34bb8246e323a3be8ac1ee9fa"
LEGACY_RELEASE_ID = "2026-09-158ec972c234"
LEGACY_VINTAGE = "2026-09"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"clave JSON duplicada en el manifiesto: {key!r}")
        seen.add(key)
    return dict(pairs)


def _legacy_bridge_problems(manifest: dict, manifest_sha: str, card_sha: str) -> list[str]:
    """El corte legado se acredita por sus CUATRO valores exactos; si alguno falla, falla todo."""
    checks = {
        "sha256 del manifiesto": (manifest_sha, LEGACY_MANIFEST_SHA256),
        "sha256 de la tarjeta": (card_sha, LEGACY_CARD_SHA256),
        "release_id": (manifest.get("release_id"), LEGACY_RELEASE_ID),
        "panel_vintage": (manifest.get("panel_vintage"), LEGACY_VINTAGE),
    }
    wrong = [f"{name}: {got!r} != {want!r}" for name, (got, want) in checks.items() if got != want]
    if wrong:
        return [
            "manifiesto sin bloque `identity` que NO es el corte legado acreditado "
            f"({'; '.join(wrong)}): todo corte nuevo debe emitirse con D4"
        ]
    print(
        f"  · identidad del release: corte LEGADO {LEGACY_RELEASE_ID} acreditado por sus cuatro "
        "valores exactos (puente cerrado, previo a D4); el próximo corte ya emite `identity`"
    )
    return []


def _identity_shape_problems(identity: object) -> list[str]:
    """El bloque `identity` se valida entero antes de usarlo. Nunca se degrada a 'no verificable'."""
    if not isinstance(identity, dict):
        return [f"identity: debe ser un objeto, no {type(identity).__name__}"]
    if set(identity) != IDENTITY_KEYS:
        return [
            f"identity: faltan {sorted(IDENTITY_KEYS - set(identity))}, sobran {sorted(set(identity) - IDENTITY_KEYS)}"
        ]
    problems: list[str] = []
    schema = identity["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != IDENTITY_SCHEMA:
        problems.append(f"identity.schema: {schema!r} no es el entero {IDENTITY_SCHEMA}")
    if identity["card_path"] != CARD_PATH:
        problems.append(f"identity.card_path: {identity['card_path']!r} != {CARD_PATH!r}")
    if identity["sentinel"] != RELEASE_ID_SENTINEL:
        problems.append(f"identity.sentinel: {identity['sentinel']!r} != {RELEASE_ID_SENTINEL!r}")
    for field in ("digest", "card_sha256_normalized"):
        value = identity[field]
        if not isinstance(value, str) or not HEX64.match(value):
            problems.append(f"identity.{field}: {value!r} no es hex minúscula de 64 caracteres")
    return problems


def release_identity_problems(root: Path = ROOT) -> list[str]:
    """Recomputa la identidad del corte y rechaza cualquier manipulación (fail-closed).

    Un manifiesto sin bloque ``identity`` sólo pasa si ES el corte legado acreditado; cualquier
    otro, o un ``identity`` mal formado, incompleto o con otro schema, es un fallo. La tarjeta se
    normaliza sustituyendo SOLO su campo estructural, nunca con un ``replace`` global.
    """
    mpath = root / MANIFEST_PATH
    if not mpath.exists():
        # Su PRESENCIA la gobierna `vp_data/contracts/release_manifest.json` en el mismo `check()`;
        # aquí solo se verifica la IDENTIDAD, así que un árbol sin manifiesto no se juzga dos veces.
        return []
    raw = mpath.read_bytes()
    try:
        manifest = json.loads(raw.decode(), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"release_manifest.json ilegible o inválido ({exc})"]
    if not isinstance(manifest, dict):
        return ["release_manifest.json: la raíz no es un objeto"]

    card_file = root / CARD_PATH
    if not card_file.exists():
        return [f"{CARD_PATH}: la tarjeta del corte no existe"]
    card = card_file.read_text()
    card_sha = hashlib.sha256(card.encode()).hexdigest()

    if "identity" not in manifest:
        return _legacy_bridge_problems(manifest, hashlib.sha256(raw).hexdigest(), card_sha)

    identity = manifest["identity"]
    problems = _identity_shape_problems(identity)
    if problems:
        return problems

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return ["release_manifest.json: `artifacts` ausente o vacío"]
    paths = [a.get("path") for a in artifacts]
    if len(set(paths)) != len(paths):
        problems.append(f"artefactos con ruta duplicada: {sorted({p for p in paths if paths.count(p) > 1})}")
    card_entries = [a for a in artifacts if a.get("path") == CARD_PATH]
    if len(card_entries) != 1:
        return [*problems, f"{CARD_PATH}: debe figurar exactamente una vez como artefacto ({len(card_entries)})"]
    if card_sha != card_entries[0].get("sha256"):
        problems.append(f"{CARD_PATH}: TARJETA MANIPULADA (sus bytes no dan el SHA-256 registrado)")

    hits = RELEASE_ID_RE.findall(card)
    if len(hits) != 1:
        return [
            *problems,
            f"{CARD_PATH}: el marcador estructural `{RELEASE_ID_FIELD.strip()}` aparece {len(hits)} veces (debe ser exactamente 1)",
        ]
    if hits[0] != manifest.get("release_id"):
        problems.append(
            f"{CARD_PATH}: ID MANIPULADO — la tarjeta declara {hits[0]!r} y el manifiesto {manifest.get('release_id')!r}"
        )

    normalized = RELEASE_ID_RE.sub(f"{RELEASE_ID_FIELD}{identity['sentinel']}", card, count=1)
    normalized_sha = hashlib.sha256(normalized.encode()).hexdigest()
    if normalized_sha != identity["card_sha256_normalized"]:
        problems.append(
            f"{CARD_PATH}: la tarjeta normalizada no reproduce `card_sha256_normalized` "
            "(marcador mal formado, sustitución global o edición fuera del campo estructural)"
        )

    pairs = [(a["path"], normalized_sha if a["path"] == CARD_PATH else a.get("sha256")) for a in artifacts]
    digest = hashlib.sha256(json.dumps(pairs, sort_keys=True).encode()).hexdigest()
    if digest != identity["digest"]:
        problems.append("identidad del release: el digest recomputado no coincide (hash de artefacto manipulado)")
    else:
        expected = f"{manifest.get('panel_vintage')}-{digest[:12]}"
        if manifest.get("release_id") != expected:
            problems.append(f"identidad del release: release_id {manifest.get('release_id')!r} != {expected!r}")
    if not problems:
        print(
            f"  · identidad del release: OK — {manifest['release_id']} deriva de la tarjeta normalizada + {len(pairs)} artefactos"
        )
    return problems


def _sha_unresolvable(root: Path, sha: str) -> list[str]:
    """[] SOLO si el sha resuelve en un historial completo. Fail-closed en todo lo demás
    (ronda 3 de auditoría: el bypass shallow anulaba la garantía exactamente donde más
    importa — CI y cron corren en checkouts de Actions)."""
    import subprocess

    try:
        shallow = subprocess.check_output(
            ["git", "rev-parse", "--is-shallow-repository"], text=True, stderr=subprocess.DEVNULL, cwd=root
        ).strip()
    except subprocess.CalledProcessError, FileNotFoundError:
        return [f"release_manifest.json: git_sha '{sha}' NO VERIFICABLE (sin git/repo) — fail-closed"]
    if shallow == "true":
        return [
            f"release_manifest.json: clone SHALLOW — git_sha '{sha}' no verificable; "
            "el checkout que corre este gate debe usar fetch-depth: 0 (fail-closed)"
        ]
    try:
        subprocess.check_call(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=root,
        )
        return []
    except subprocess.CalledProcessError:
        return [f"release_manifest.json: git_sha '{sha}' NO existe en el historial (commit fantasma)"]


def main() -> int:
    problems = check()
    if problems:
        print(f"✗ CONTRATOS ROTOS — {len(problems)} problema(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    n = len(list(CONTRACTS_DIR.glob("*.json")))
    print(f"✓ Contratos OK — {n} artefactos validados, añada única {_panel_vintage(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

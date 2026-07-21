#!/usr/bin/env python
"""B284/B290: registro POSITIVO de TODA instalación pip de CI. Escanea estructuralmente (loader anti-claves-duplicadas)
los 5 workflows y exige una BIYECCIÓN de multiconjunto entre las instalaciones observadas y
`security/ci_install_registry.json`:

- reconoce `pip`/`pip3`/`uv pip`/`python -m pip`/`<venv>/bin/python -m pip install` y el bootstrap gobernado
  (`install_governance_bootstrap.py`);
- identidad = (workflow, job, comando-normalizado) con CONTEO — un install movido/nuevo/borrado/duplicado rompe la
  biyección;
- categorías CERRADAS: `hashed-lock`, `editable-after-lock-no-deps`, `governance-bootstrap`, `auditor-tool`,
  `pinned-toolchain`, `constrained-editable-lock`, `temporary-deferred` (esta ÚLTIMA exige `expires` no expirado);
- un install ESCONDIDO en `eval`/`bash -c`/`sh -c`/heredoc/backtick/`$(…)` (junto a una firma de instalación) = ROJO;
- ninguna categoría fuera del set cerrado; entrada sin razón/categoría = ROJO.

`--generate` imprime el candidato observado (NUNCA modifica el registro). Requiere PyYAML (por eso corre bajo el venv del
bootstrap gobernado). Fail-closed."""

from __future__ import annotations

import datetime
import json
import os
import re
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKFLOWS_DIR = ".github/workflows"
_REGISTRY = "security/ci_install_registry.json"
_TOP_KEYS = {"schema_version", "note", "categories", "installs"}
_ENTRY_KEYS = {"workflow", "job", "command", "count", "category", "reason", "expires"}
_SCHEMA_VERSION = 1
_CATEGORIES = (
    "hashed-lock",
    "editable-after-lock-no-deps",
    "governance-bootstrap",
    "auditor-tool",
    "pinned-toolchain",
    "constrained-editable-lock",
    "temporary-deferred",
)
_BOOTSTRAP_SIG = "install_governance_bootstrap.py"
# firma de una instalación pip en CUALQUIER forma reconocida
_INSTALL_SIG = re.compile(r"(?:\bpip3?\s+install\b)|(?:\buv\s+pip\s+install\b)|(?:-m\s+pip\s+install\b)")
# comandos que EVALÚAN dinámicamente su argumento (una firma de install DENTRO de ellos es un install oculto)
_DYN_EXEC = re.compile(r"^(?:eval\b|bash\s+-c\b|sh\s+-c\b)")
# un shell que lee un heredoc (`bash <<X` / `sh <<X`): su cuerpo puede alimentar un install
_SHELL_HEREDOC = re.compile(r"\b(?:bash|sh)\b[^\n]*<<")


class _NoDupLoader(yaml.SafeLoader):
    pass


def _no_dup_mapping(loader: _NoDupLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(None, None, f"clave YAML duplicada: {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_NoDupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def _norm(cmd: str) -> str:
    """Normaliza un comando: quita comentario shell final (` #…`) y colapsa espacios."""
    cmd = re.sub(r"\s+#\s.*$", "", cmd)
    return re.sub(r"\s+", " ", cmd).strip()


def _simple_commands(run: str) -> list[str]:
    """Divide un bloque `run` en comandos simples por salto de línea, `&&`, `;`, `|` (aprox. suficiente para CI)."""
    parts: list[str] = []
    for line in run.split("\n"):
        for seg in re.split(r"&&|\|\||;|\|", line):
            seg = seg.strip()
            if seg:
                parts.append(seg)
    return parts


def _is_install(cmd: str) -> bool:
    return bool(_INSTALL_SIG.search(cmd)) or _BOOTSTRAP_SIG in cmd


def _strip_comments(run: str) -> str:
    """Quita comentarios shell (`#` al inicio de palabra) por línea — evita que un backtick/`$(…)` en un COMENTARIO
    dispare la detección de ofuscación. Heurística suficiente para bloques `run` de CI."""
    out: list[str] = []
    for line in run.split("\n"):
        m = re.search(r"(?:^|\s)#", line)
        out.append(line[: m.start()] if m else line)
    return "\n".join(out)


def _substitution_spans(s: str) -> list[str]:
    """Contenidos de `$(…)` (balanceado) y de `` `…` `` — donde un install ESCONDIDO viviría."""
    spans: list[str] = []
    i = 0
    while i < len(s):
        if s[i : i + 2] == "$(":
            depth, j = 1, i + 2
            while j < len(s) and depth:
                depth += (s[j] == "(") - (s[j] == ")")
                j += 1
            spans.append(s[i + 2 : j - 1])
            i = j
        elif s[i] == "`":
            j = s.find("`", i + 1)
            if j == -1:
                break
            spans.append(s[i + 1 : j])
            i = j + 1
        else:
            i += 1
    return spans


def _hidden_install(stripped: str) -> bool:
    """True si una firma de `pip install` vive DENTRO de una construcción de ofuscación: substitución `$(…)`/backtick,
    argumento de `eval`/`bash -c`/`sh -c`, o el cuerpo de un heredoc a un shell. Un `$(tail -1 …)` o `stale=$(python …)`
    legítimo NO dispara (no contiene la firma); un `pip install` en su propia línea tampoco (no está ofuscado)."""
    if any(_INSTALL_SIG.search(span) for span in _substitution_spans(stripped)):
        return True
    for line in stripped.split("\n"):
        for cmd in re.split(r"&&|\|\||;|\|", line):
            if _DYN_EXEC.match(cmd.strip()) and _INSTALL_SIG.search(cmd):
                return True
    return bool(_SHELL_HEREDOC.search(stripped) and _INSTALL_SIG.search(stripped))


def _scan_workflows() -> tuple[dict[tuple[str, str, str], int], list[str]]:
    """Devuelve `(multiconjunto observado {(wf,job,cmd):conteo}, problemas)`. Fail-closed ante YAML ilegible/duplicado o
    install escondido."""
    observed: dict[tuple[str, str, str], int] = {}
    problems: list[str] = []
    try:
        names = sorted(f for f in os.listdir(os.path.join(_ROOT, _WORKFLOWS_DIR)) if f.endswith((".yml", ".yaml")))
    except OSError as exc:
        return {}, [f"{_WORKFLOWS_DIR}: ilegible ({exc}) (fail-closed)"]
    if not names:
        return {}, [f"{_WORKFLOWS_DIR}: sin workflows (fail-closed)"]
    for name in names:
        path = os.path.join(_ROOT, _WORKFLOWS_DIR, name)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = yaml.load(fh, Loader=_NoDupLoader)
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"{name}: YAML ilegible/duplicado ({exc}) (fail-closed)")
            continue
        jobs = (doc or {}).get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                run = step.get("run") if isinstance(step, dict) else None
                if not isinstance(run, str):
                    continue
                # Bloque OCULTO: una firma de `pip install` DENTRO de una construcción de ofuscación (substitución
                # `$(…)`/backtick, argumento de eval/bash -c/sh -c, o cuerpo de heredoc a un shell) → ROJO, y no se
                # capturan installs de aquí. Un `$(tail …)`/`stale=$(python …)` legítimo NO dispara (no lleva la firma);
                # el bootstrap `$(install_governance_bootstrap.py)` tampoco (no lleva firma pip).
                stripped = _strip_comments(run)
                if _hidden_install(stripped):
                    problems.append(f"{name}::{job_name}: firma de `pip install` dentro de $()/backtick/eval/bash -c/sh -c/heredoc (install oculto/no comprendido, fail-closed B290)")  # fmt: skip
                    continue
                for cmd in _simple_commands(run):
                    if _is_install(cmd):
                        key = (name, str(job_name), _norm(cmd))
                        observed[key] = observed.get(key, 0) + 1
    return observed, problems


def _load_registry() -> tuple[dict, list[str]]:
    try:
        with open(os.path.join(_ROOT, _REGISTRY), encoding="utf-8") as fh:
            return json.loads(fh.read()), []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{_REGISTRY}: ilegible/no-JSON ({exc}) (fail-closed)"]


def _registered_multiset(reg: dict) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], dict], list[str]]:  # fmt: skip
    problems: list[str] = []
    multiset: dict[tuple[str, str, str], int] = {}
    by_key: dict[tuple[str, str, str], dict] = {}
    today = datetime.date.today()
    for e in reg["installs"]:
        if not (isinstance(e, dict) and set(e) == _ENTRY_KEYS):
            problems.append(f"{_REGISTRY}: entrada con claves != {sorted(_ENTRY_KEYS)}: {e!r}")
            continue
        key = (e["workflow"], e["job"], e["command"])
        if not (type(e["count"]) is int and e["count"] >= 1):
            problems.append(f"{_REGISTRY}[{key}]: count no es int>=1")
        if e["category"] not in _CATEGORIES:
            problems.append(f"{_REGISTRY}[{key}]: categoría {e['category']!r} fuera de {list(_CATEGORIES)}")
        if not (isinstance(e["reason"], str) and e["reason"].strip()):
            problems.append(f"{_REGISTRY}[{key}]: reason vacía")
        exp = e["expires"]
        if e["category"] == "temporary-deferred":
            try:
                exp_date = datetime.date.fromisoformat(exp) if isinstance(exp, str) else None
            except ValueError:
                exp_date = None
            if exp_date is None:
                problems.append(f"{_REGISTRY}[{key}]: temporary-deferred exige expires ISO")
            elif exp_date < today:
                problems.append(
                    f"{_REGISTRY}[{key}]: temporary-deferred EXPIRADO ({exp}) — re-hardening o retiro (B290)"
                )
        elif exp is not None:
            problems.append(f"{_REGISTRY}[{key}]: sólo temporary-deferred lleva expires (debe ser null)")
        if key in by_key:
            problems.append(f"{_REGISTRY}[{key}]: entrada duplicada (usa count)")
        by_key[key] = e
        multiset[key] = multiset.get(key, 0) + (e["count"] if type(e["count"]) is int else 0)
    return multiset, by_key, problems


def problems() -> list[str]:
    reg, errs = _load_registry()
    if errs:
        return errs
    if not (isinstance(reg, dict) and set(reg) == _TOP_KEYS):
        return [f"{_REGISTRY}: claves superiores != {sorted(_TOP_KEYS)} (fail-closed)"]
    if not (type(reg["schema_version"]) is int and reg["schema_version"] == _SCHEMA_VERSION):
        return [f"{_REGISTRY}: schema_version != {_SCHEMA_VERSION}"]
    if list(reg["categories"]) != list(_CATEGORIES):
        return [f"{_REGISTRY}: categories != el set cerrado del gate {list(_CATEGORIES)}"]
    if not isinstance(reg["installs"], list):
        return [f"{_REGISTRY}: 'installs' no es lista"]
    observed, oprobs = _scan_workflows()
    registered, _by_key, rprobs = _registered_multiset(reg)
    problems = oprobs + rprobs
    for key in sorted(observed.keys() | registered.keys()):
        obs, rgs = observed.get(key, 0), registered.get(key, 0)
        if obs != rgs:
            wf, job, cmd = key
            if rgs == 0:
                problems.append(
                    f"INSTALL NO REGISTRADO: {wf}::{job}: `{cmd}` (×{obs}) (registrar en {_REGISTRY}) (B290)"
                )
            elif obs == 0:
                problems.append(f"install REGISTRADO OBSOLETO: {wf}::{job}: `{cmd}` (ya no aparece) (B290)")
            else:
                problems.append(f"conteo divergente {wf}::{job}: `{cmd}` observado ×{obs} != registrado ×{rgs} (B290)")
    return problems


def _generate() -> int:
    observed, oprobs = _scan_workflows()
    for p in oprobs:
        print(f"# WARN {p}", file=sys.stderr)
    placeholder = "POR-CLASIFICAR"  # el humano rellena categoría/razón; texto neutro (no dispara el trinquete de deuda)
    installs = [
        {
            "workflow": wf,
            "job": job,
            "command": cmd,
            "count": n,
            "category": placeholder,
            "reason": placeholder,
            "expires": None,
        }  # fmt: skip
        for (wf, job, cmd), n in sorted(observed.items())
    ]
    print(json.dumps({"schema_version": _SCHEMA_VERSION, "note": placeholder, "categories": list(_CATEGORIES), "installs": installs}, indent=2, ensure_ascii=False))  # fmt: skip
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--generate":
        return _generate()
    probs = problems()
    if probs:
        print("✗ registro de instalaciones de CI violado:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ biyección exacta de instalaciones de CI ({_REGISTRY})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

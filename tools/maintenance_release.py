"""Acta propia de mantenimiento: CI/autoridad reales, registro único; nunca despliega web.

El corte nominal sólo cambia Git; sus punteros DVC ya estaban publicados. Consumir
registra la aceptación del corte integrado, no empuja Git/DVC ni toca la campaña.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from tools import check_maintenance_release as check
from tools import maintenance_registry as storage
from tools.maintenance_github import GitHub, encoded

BASE = "a6036ec20eaf5ecb17b1cce43fe2ecea9436a82b"
BASE_RELEASE = "2026-09-87a5be76e8a1"
BASE_MANIFEST_SHA256 = "9460c77e053da99ee1a2a225becd8f4b018a34c62412900c9eee7f1ab471c369"
ORIGINS = ("https://github.com/UACJ-MIAAD/VisaPredictAI.git", "git@github.com:UACJ-MIAAD/VisaPredictAI.git")
Blocked = check.MaintenanceError


def boundary[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    @functools.wraps(function)
    def run(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return function(*args, **kwargs)
        except (OSError, subprocess.SubprocessError, KeyError, TypeError, UnicodeError) as exc:
            raise Blocked(f"operación bloqueada: {type(exc).__name__}: {exc}") from exc

    return run


initialize = boundary(storage.initialize)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def authorization_request(publisher_id: str, review_raw: bytes) -> dict:
    return {
        "schema": "metadata-maintenance-authorization/1",
        "decision": "autorizar-consumo-mantenimiento",
        "publisher_id": publisher_id,
        "review_sha256": _sha(review_raw),
    }


def _policy(repo: Path, inspection: dict) -> None:
    if inspection["base_commit"] != BASE or inspection["base_release_id"] != BASE_RELEASE:
        raise Blocked("base distinta de la adoptada en ADR 0005")
    if _sha(check._git(repo, "show", BASE + ":" + check.MANIFEST)) != BASE_MANIFEST_SHA256:
        raise Blocked("el manifiesto base no es el publicado adoptado")


@boundary
def inspect_candidate(repo: Path, candidate: str) -> dict:
    result = check.inspect(repo, BASE, candidate)
    _policy(repo, result)
    return result


def _filename(release: str) -> str:
    if not isinstance(release, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9a-f]{12}", release):
        raise Blocked("release inválida")
    return release + ".json"


def _proofs(proofs: object, tree: object) -> None:
    if (
        not isinstance(proofs, list)
        or len(proofs) != len(check.CI_JOBS)
        or not all(isinstance(p, dict) for p in proofs)
    ):
        raise Blocked("pruebas de jobs incompletas")
    if {p.get("job") for p in proofs} != check.CI_JOBS:
        raise Blocked("identidades de jobs distintas")
    for proof in proofs:
        if set(proof) != {"job", "job_id", "checkout_commit", "tested_tree_sha", "log_sha256"}:
            raise Blocked("esquema de prueba de job inválido")
        if type(proof["job_id"]) is not int or proof["job_id"] <= 0 or proof["tested_tree_sha"] != tree:
            raise Blocked("árbol o identidad del job inválidos")
        if not isinstance(proof["checkout_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", proof["checkout_commit"]):
            raise Blocked("commit de checkout inválido")
        if not isinstance(proof["log_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", proof["log_sha256"]):
            raise Blocked("hash del log inválido")


def _record(raw: bytes, publisher: str, release: str) -> dict:
    record = check._json(raw)
    if set(record) != {"schema", "publisher_id", "release_id", "revision", "status", "head_sha256", "events"}:
        raise Blocked("esquema cerrado del registro inválido")
    events = record["events"]
    if (
        record["schema"] != "metadata-maintenance-state/1"
        or record["publisher_id"] != publisher
        or record["release_id"] != release
    ):
        raise Blocked("registro de otro publicador/release")
    if (
        not isinstance(events, list)
        or not 1 <= len(events) <= 128
        or type(record["revision"]) is not int
        or record["revision"] != len(events) - 1
    ):
        raise Blocked("revisión/eventos inválidos")
    previous, terminal, identity, timestamp = "0" * 64, False, None, None
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {"revision", "status", "at", "previous_sha256", "payload"}:
            raise Blocked("evento inválido")
        if (
            type(event["revision"]) is not int
            or event["revision"] != index
            or event["previous_sha256"] != previous
            or terminal
        ):
            raise Blocked("cadena de eventos rota o permiso ya consumido")
        current_time = check._date(event["at"])
        if timestamp is not None and current_time < timestamp:
            raise Blocked("historial temporal inválido")
        timestamp = current_time
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise Blocked("payload inválido")
        if event["status"] == "approved":
            if set(payload) != {
                "inspection",
                "review_raw",
                "review_sha256",
                "ci_capture_sha256",
                "ci_capture_raw",
                "job_proofs",
                "comment_id",
                "comment_sha256",
                "comment_raw",
            }:
                raise Blocked("autorización registrada inválida")
            if (
                not isinstance(payload["review_raw"], str)
                or _sha(payload["review_raw"].encode()) != payload["review_sha256"]
            ):
                raise Blocked("bytes de revisión cambiados")
            inspection = payload["inspection"]
            if not isinstance(inspection, dict) or inspection.get("release_id") != release:
                raise Blocked("inspección registrada inválida")
            _proofs(payload["job_proofs"], inspection["candidate_tree"])
            bound = [inspection[k] for k in check.REVIEW_BINDINGS]
            if identity is not None and identity != bound:
                raise Blocked("identidad cambió en el historial")
            identity = bound
            if type(payload["comment_id"]) is not int or payload["comment_id"] <= 0:
                raise Blocked("comentario inválido")
            for key in ("review_sha256", "ci_capture_sha256", "comment_sha256"):
                if not isinstance(payload[key], str) or not re.fullmatch(r"[0-9a-f]{64}", payload[key]):
                    raise Blocked("hash inválido")
            for key, digest in (("ci_capture_raw", "ci_capture_sha256"), ("comment_raw", "comment_sha256")):
                if not isinstance(payload[key], str) or _sha(payload[key].encode()) != payload[digest]:
                    raise Blocked("bytes de evidencia cambiados")
        elif event["status"] == "consumed" and index > 0:
            if set(payload) != {
                "release_commit",
                "integration_run_id",
                "ci_capture_sha256",
                "ci_capture_raw",
                "job_proofs",
            }:
                raise Blocked("consumo registrado inválido")
            if (
                not re.fullmatch(r"[0-9a-f]{40}", payload["release_commit"])
                or type(payload["integration_run_id"]) is not int
                or payload["integration_run_id"] <= 0
            ):
                raise Blocked("identidad del consumo inválida")
            if not re.fullmatch(r"[0-9a-f]{64}", payload["ci_capture_sha256"]):
                raise Blocked("hash CI inválido")
            if (
                not isinstance(payload["ci_capture_raw"], str)
                or _sha(payload["ci_capture_raw"].encode()) != payload["ci_capture_sha256"]
            ):
                raise Blocked("bytes CI cambiados")
            _proofs(payload["job_proofs"], events[index - 1]["payload"]["inspection"]["candidate_tree"])
            terminal = True
        else:
            raise Blocked("transición inválida")
        previous = _sha(encoded(event))
    if record["status"] != events[-1]["status"] or record["head_sha256"] != previous:
        raise Blocked("huella del registro cambiada")
    return record


def _append(record: dict | None, publisher: str, release: str, status: str, payload: dict) -> dict:
    events = [] if record is None else list(record["events"])
    event = {
        "revision": len(events),
        "status": status,
        "at": datetime.now(UTC).isoformat(),
        "previous_sha256": "0" * 64 if record is None else record["head_sha256"],
        "payload": payload,
    }
    events.append(event)
    result = {
        "schema": "metadata-maintenance-state/1",
        "publisher_id": publisher,
        "release_id": release,
        "revision": len(events) - 1,
        "status": status,
        "head_sha256": _sha(encoded(event)),
        "events": events,
    }
    return _record(encoded(result), publisher, release)


def _evidence(
    github: GitHub, inspection: dict, publisher: str, raw: bytes, run_id: int, comment_id: int
) -> tuple[dict, dict]:
    if len(raw) > 65536:
        raise Blocked("revisión demasiado grande")
    review = check._json(raw)
    if review.get("reviewed_by") != "Javier Rebull":
        raise Blocked("revisor distinto del autor autorizado")
    capture = github.capture(run_id, inspection["candidate_commit"], inspection["candidate_tree"])
    hashes = check.check_review_bindings(inspection, raw, capture)
    comment_raw, pull = github.authorization(
        comment_id, authorization_request(publisher, raw), inspection["candidate_commit"], review
    )
    return {
        "inspection": inspection,
        "review_raw": raw.decode(),
        "review_sha256": hashes["review_sha256"],
        "ci_capture_sha256": hashes["ci_capture_sha256"],
        "ci_capture_raw": capture.decode(),
        "job_proofs": github.last_proofs,
        "comment_id": comment_id,
        "comment_sha256": _sha(comment_raw),
        "comment_raw": comment_raw.decode(),
    }, pull


@boundary
def register(repo: Path, registry: Path, candidate: str, review_raw: bytes, run_id: int, comment_id: int) -> dict:
    with storage.Registry(registry) as state:
        _, publisher = state.identity()
        inspection = inspect_candidate(repo, candidate)
        release = inspection["release_id"]
        name = _filename(release)
        before = state.read(name)
        old = None if before is None else _record(before, publisher, release)
        if old is not None and old["status"] == "consumed":
            raise Blocked("permiso ya consumido")
        payload, _ = _evidence(GitHub(), inspection, publisher, review_raw, run_id, comment_id)
        if old is not None:
            previous = old["events"][-1]["payload"]
            if previous == payload:
                return old
            if any(previous["inspection"][key] != inspection[key] for key in check.REVIEW_BINDINGS):
                raise Blocked("no se reemplaza por otro candidato bajo la misma release")
            if check._date(check._json(review_raw)["reviewed_at"]) <= check._date(
                check._json(previous["review_raw"].encode())["reviewed_at"]
            ):
                raise Blocked("la nueva revisión no es posterior a la anterior")
        result = _append(old, publisher, release, "approved", payload)
        state.write(name, result, before)
        return result


def _integrated(repo: Path, inspection: dict, github: GitHub, pull: dict, run_id: int) -> tuple[str, bytes]:
    head = check._git(repo, "rev-parse", "HEAD").decode().strip()
    if check._git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        raise Blocked("checkout sucio")
    if check._git(repo, "rev-parse", "HEAD^{tree}").decode().strip() != inspection["candidate_tree"]:
        raise Blocked("HEAD no tiene el árbol aprobado")
    if pull.get("merged") is not True or pull.get("merge_commit_sha") != head:
        raise Blocked("PR no integrada en el commit que se consume")
    if check._git(repo, "remote", "get-url", "origin").decode().strip() not in ORIGINS:
        raise Blocked("origin no es el repositorio adoptado")
    tag = "refs/tags/release/" + inspection["release_id"]
    rows = (
        check._git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/main", tag, tag + "^{}")
        .decode()
        .splitlines()
    )
    refs = {row.split()[1]: row.split()[0] for row in rows}
    if refs.get("refs/heads/main") != head:
        raise Blocked("HEAD no es main remoto vigente")
    if tag not in refs or refs.get(tag + "^{}") != inspection["source_commit_full"]:
        raise Blocked("falta tag anotado sobre el origen aprobado")
    return head, github.capture(run_id, head, inspection["candidate_tree"])


@boundary
def consume(repo: Path, registry: Path, release: str, integration_run_id: int) -> dict:
    with storage.Registry(registry) as state:
        _, publisher = state.identity()
        name = _filename(release)
        before = state.read(name)
        if before is None:
            raise Blocked("release sin autorización registrada")
        old = _record(before, publisher, release)
        if old["status"] == "consumed":
            raise Blocked("permiso ya consumido")
        approval = old["events"][-1]["payload"]
        inspection = check.inspect_committed(repo, BASE, approval["inspection"]["candidate_commit"])
        _policy(repo, inspection)
        if inspection != approval["inspection"]:
            raise Blocked("candidato cambió desde la aprobación")
        github = GitHub()
        review_raw = approval["review_raw"].encode()
        review = check._json(review_raw)
        verified, pull = _evidence(
            github, inspection, publisher, review_raw, review["ci_run_id"], approval["comment_id"]
        )
        if verified != approval:
            raise Blocked("evidencia cambió desde el registro")
        head, capture = _integrated(repo, inspection, github, pull, integration_run_id)
        result = _append(
            old,
            publisher,
            release,
            "consumed",
            {
                "release_commit": head,
                "integration_run_id": integration_run_id,
                "ci_capture_sha256": _sha(capture),
                "ci_capture_raw": capture.decode(),
                "job_proofs": github.last_proofs,
            },
        )
        # No efecto de red después de esta puerta: aceptación propia del corte Git.
        state.write(name, result, before)
        return result


@boundary
def status(registry: Path, release: str) -> dict:
    with storage.Registry(registry) as state:
        _, publisher = state.identity()
        raw = state.read(_filename(release))
        if raw is None:
            raise Blocked("release sin registro")
        return _record(raw, publisher, release)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--registry", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--candidate", required=True)
    inspect.add_argument("--review", type=Path)
    registration = commands.add_parser("register")
    registration.add_argument("--candidate", required=True)
    registration.add_argument("--review", type=Path, required=True)
    registration.add_argument("--run-id", type=int, required=True)
    registration.add_argument("--comment-id", type=int, required=True)
    consumption = commands.add_parser("consume")
    consumption.add_argument("--release", required=True)
    consumption.add_argument("--integration-run-id", type=int, required=True)
    query = commands.add_parser("status")
    query.add_argument("--release", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.registry)
        elif args.command == "inspect":
            result = inspect_candidate(args.repo, args.candidate)
            if args.review:
                with storage.Registry(args.registry) as registry:
                    _, publisher = registry.identity()
                result["authorization_request"] = authorization_request(publisher, args.review.read_bytes())
        elif args.command == "register":
            result = register(
                args.repo, args.registry, args.candidate, args.review.read_bytes(), args.run_id, args.comment_id
            )
        elif args.command == "consume":
            result = consume(args.repo, args.registry, args.release, args.integration_run_id)
        else:
            result = status(args.registry, args.release)
    except (Blocked, OSError) as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

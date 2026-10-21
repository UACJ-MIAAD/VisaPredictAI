"""Autoridades de mantenimiento leídas en GitHub; GET únicamente, nunca publica mensajes."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess

from tools import check_maintenance_release as check

REPOSITORY = "UACJ-MIAAD/VisaPredictAI"
STEP = "Record maintenance checkout identity"
AUTHOR_ID = 168046724
MARKER = re.compile(r"(?:^|\s)VP_MAINTENANCE_CHECKOUT ([0-9a-f]{40}) ([0-9a-f]{40})\s*$", re.MULTILINE)
loads = check._json


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise check.MaintenanceError("GitHub devolvió un objeto de tipo incorrecto")
    return value


def encoded(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


class GitHub:
    last_proofs: list[dict]

    def read(self, path: str) -> bytes:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", "GET", f"repos/{REPOSITORY}/{path}"],
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode or len(result.stdout) > 32 * 1024 * 1024:
            raise check.MaintenanceError("GitHub no pudo acreditar la evidencia solicitada")
        return result.stdout

    def capture(self, run_id: int, commit: str, tree: str) -> bytes:
        if type(run_id) is not int or run_id <= 0:
            raise check.MaintenanceError("corrida CI inválida")
        run = loads(self.read(f"actions/runs/{run_id}"))
        if type(run.get("id")) is not int:
            raise check.MaintenanceError("identidad de corrida inválida")
        if (
            run.get("id"),
            _object(run.get("repository")).get("full_name"),
            run.get("path"),
            run.get("status"),
            run.get("conclusion"),
            run.get("head_sha"),
        ) != (run_id, REPOSITORY, ".github/workflows/ci.yml", "completed", "success", commit):
            raise check.MaintenanceError("CI remoto no verde o de otro commit/repo/workflow")
        attempt = run.get("run_attempt")
        if type(attempt) is not int or attempt <= 0:
            raise check.MaintenanceError("intento CI inválido")
        response = loads(self.read(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"))
        jobs = response.get("jobs")
        if (
            not isinstance(jobs, list)
            or response.get("total_count") != len(jobs)
            or not all(isinstance(j, dict) for j in jobs)
        ):
            raise check.MaintenanceError("lista de jobs incompleta")
        proofs = []
        for name in sorted(check.CI_JOBS):
            found = [j for j in jobs if j.get("name") == name]
            if len(found) != 1:
                raise check.MaintenanceError(f"job ausente o duplicado: {name}")
            job = found[0]
            if type(job.get("run_id")) is not int or (job.get("run_id"), job.get("status"), job.get("conclusion")) != (
                run_id,
                "completed",
                "success",
            ):
                raise check.MaintenanceError(f"job no verde: {name}")
            steps = job.get("steps")
            if (
                not isinstance(steps, list)
                or not all(isinstance(s, dict) for s in steps)
                or not any(s.get("name") == STEP and s.get("conclusion") == "success" for s in steps)
            ):
                raise check.MaintenanceError(f"sin captura del checkout: {name}")
            job_id = job.get("id")
            if type(job_id) is not int or job_id <= 0:
                raise check.MaintenanceError("identidad de job inválida")
            log = self.read(f"actions/jobs/{job_id}/logs")
            markers = MARKER.findall(log.decode("utf-8"))
            if len(markers) != 1 or markers[0][1] != tree:
                raise check.MaintenanceError(f"árbol ensayado distinto o ambiguo: {name}")
            checkout = loads(self.read(f"git/commits/{markers[0][0]}"))
            if checkout.get("sha") != markers[0][0] or _object(checkout.get("tree")).get("sha") != tree:
                raise check.MaintenanceError("GitHub no confirma el árbol del checkout ensayado")
            proofs.append(
                {
                    "job": name,
                    "job_id": job_id,
                    "checkout_commit": markers[0][0],
                    "tested_tree_sha": tree,
                    "log_sha256": hashlib.sha256(log).hexdigest(),
                }
            )
        check._date(run["updated_at"])
        self.last_proofs = proofs
        return encoded(
            {
                "schema": "metadata-maintenance-ci-capture/1",
                "repository": REPOSITORY,
                "workflow": ".github/workflows/ci.yml",
                "head_sha": commit,
                "tested_tree_sha": tree,
                "run_id": run_id,
                "completed_at": run["updated_at"],
                "jobs": dict.fromkeys(sorted(check.CI_JOBS), "success"),
            }
        )

    def authorization(self, comment_id: int, expected: dict, candidate: str, review: dict) -> tuple[bytes, dict]:
        if type(comment_id) is not int or comment_id <= 0:
            raise check.MaintenanceError("comentario de autorización inválido")
        comment = loads(self.read(f"issues/comments/{comment_id}"))
        user = _object(comment.get("user"))
        if type(user.get("id")) is not int or user.get("id") != AUTHOR_ID or user.get("type") != "User":
            raise check.MaintenanceError("el comentario no pertenece al autor humano autorizado")
        if (
            type(comment.get("id")) is not int
            or comment.get("id") != comment_id
            or not isinstance(comment.get("body"), str)
            or loads(comment["body"].encode()) != expected
        ):
            raise check.MaintenanceError("el comentario no autoriza esta revisión/publicador")
        issue = re.fullmatch(rf"https://api.github.com/repos/{REPOSITORY}/issues/([1-9][0-9]*)", comment["issue_url"])
        if issue is None:
            raise check.MaintenanceError("autorización fuera de la PR del repositorio")
        pull = loads(self.read(f"pulls/{issue[1]}"))
        if (
            _object(pull.get("head")).get("sha"),
            _object(pull.get("base")).get("ref"),
            _object(_object(pull.get("base")).get("repo")).get("full_name"),
        ) != (candidate, "main", REPOSITORY):
            raise check.MaintenanceError("la PR no contiene el candidato revisado contra main")
        if check._date(comment["created_at"]) < check._date(review["reviewed_at"]):
            raise check.MaintenanceError("autorización anterior a la revisión")
        check._date(comment["updated_at"])
        bound = {k: comment[k] for k in ("id", "body", "created_at", "updated_at", "issue_url")}
        bound["user"] = {k: user[k] for k in ("id", "type")}
        return encoded(bound), pull

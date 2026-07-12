"""A6 — la escritura JSONL es transaccional bajo concurrencia (flock + append + fsync).

Dos niveles: N PROCESOS reales escribiendo al mismo archivo de staging (el caso del
diagnóstico: corrupción concurrente) y N threads dentro de un proceso. En ambos casos
ningún record se pierde, ninguna línea se mezcla (todas parsean como JSON) y todas las
claves de evento son únicas.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

from vp_data import tracking

ROOT = Path(__file__).resolve().parent.parent

N_PROCS = 5
N_PER_PROC = 30

_CHILD = """
import json, sys, time
from pathlib import Path

sys.path.insert(0, {root!r})
from vp_data import tracking

tracking.STAGING = Path({staging!r})
# stub de git para no pagar ~2 subprocess por record (el lock es lo que se prueba)
tracking.git_state = lambda: ("stub", False)
tracking.code_sha.cache_clear()
tracking.code_sha = lambda: "stub" * 10

wid = int(sys.argv[1])
go = Path({staging!r}) / ".go"
for _ in range(500):           # barrera: todos los escritores arrancan a la vez
    if go.exists():
        break
    time.sleep(0.01)
for i in range({n}):
    tracking.log_run("conc", f"w{{wid}}-r{{i}}", {{"wid": wid, "i": i, "pad": "x" * 300}}, {{"m": float(i)}})
"""


def test_parallel_processes_do_not_lose_or_interleave_records(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    script = _CHILD.format(root=str(ROOT), staging=str(staging), n=N_PER_PROC)
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(w)], cwd=ROOT, stderr=subprocess.PIPE)
        for w in range(N_PROCS)
    ]
    (staging / ".go").touch()  # suelta la barrera con todos los procesos vivos
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()

    lines = (staging / "conc.jsonl").read_text().splitlines()
    assert len(lines) == N_PROCS * N_PER_PROC  # nada se pierde
    recs = [json.loads(line) for line in lines]  # nada se mezcla (todas parsean)
    assert len({r["rec_id"] for r in recs}) == len(recs)  # claves de evento únicas
    written = {(r["params"]["wid"], r["params"]["i"]) for r in recs}
    assert written == {(w, i) for w in range(N_PROCS) for i in range(N_PER_PROC)}  # set completo
    assert all(r["tags"]["pipeline_run_id"] for r in recs)  # A6: 100 % con pipeline_run_id


def test_parallel_threads_do_not_lose_or_interleave_records(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    monkeypatch.setattr(tracking, "STAGING", staging)
    monkeypatch.setattr(tracking, "git_state", lambda: ("stub", False))
    n_threads, n_per = 8, 20
    start = threading.Barrier(n_threads)

    def writer(wid: int) -> None:
        start.wait()
        for i in range(n_per):
            tracking.log_run("conc_t", f"w{wid}-r{i}", {"wid": wid, "i": i}, {"m": float(i)})

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)

    lines = (staging / "conc_t.jsonl").read_text().splitlines()
    assert len(lines) == n_threads * n_per
    recs = [json.loads(line) for line in lines]
    assert len({r["rec_id"] for r in recs}) == len(recs)
    assert {(r["params"]["wid"], r["params"]["i"]) for r in recs} == {
        (w, i) for w in range(n_threads) for i in range(n_per)
    }


def test_lock_helper_appends_flushes_and_unlocks(tmp_path):
    """Sanity del helper: append serializado, contenido íntegro, archivo reutilizable."""
    path = tmp_path / "x.jsonl"
    for i in range(50):
        tracking._locked_append(path, json.dumps({"i": i}) + "\n")
    lines = path.read_text().splitlines()
    assert [json.loads(x)["i"] for x in lines] == list(range(50))

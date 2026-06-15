# DVC en este repositorio

DVC está **inicializado** (`dvc init`) pero deliberadamente **no versiona los CSV
abiertos**. Esta nota explica por qué y para qué se reserva, adaptando la práctica
del repo hermano EpiForecast-MX al caso de visas.

## Decisión: los CSV abiertos se quedan en git

Los CSV por país y el panel (`data/*.csv`) son el **entregable de datos abiertos**
del proyecto: cualquiera los descarga directamente del repositorio, sin necesitar
DVC ni credenciales de un remoto S3. Versionarlos con DVC los sacaría de git y
**rompería esa accesibilidad**. El `.dvcignore` los protege explícitamente para que
un `dvc add data/` accidental nunca los mueva. El *bloat* histórico de git venía de
las **figuras binarias**, ya resueltas (gitignored; regenerar con `make figures`).

## Para qué se reserva DVC (próximo semestre, modelado)

DVC es la herramienta correcta para los artefactos que llegarán con la fase de
modelado, igual que en EpiForecast-MX (que versiona `models.dvc`, `checkpoints/`,
PDFs grandes en S3):

- **checkpoints de modelos** (LSTM, ARIMA-LSTM, DeepAR…)
- **pronósticos y artefactos grandes** que no deben vivir en git
- **datasets derivados pesados** (features, splits walk-forward)

## Cómo activarlo cuando haga falta

```bash
# 1) provisionar un remoto (S3, GCS, GDrive…) y configurarlo
dvc remote add -d storage s3://<bucket>/visapredict
dvc remote modify storage region us-east-1

# 2) versionar un artefacto de modelo (NO los CSV abiertos)
dvc add checkpoints/best_model.ckpt
git add checkpoints/best_model.ckpt.dvc checkpoints/.gitignore
git commit -m "data: track model checkpoint with DVC"

# 3) subir/bajar
dvc push        # sube al remoto
dvc pull        # baja en otro clon

# en la GitHub Action, las credenciales del remoto van como secrets.
```

`dvc` es una dependencia de desarrollo (no se necesita en runtime ni en el CI de
código hasta que existan artefactos versionados).

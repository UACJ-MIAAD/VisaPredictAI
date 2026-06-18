# Bundle GPU/AWS — frontier pesado + confirmación multi-semilla

Corre en **GPU** lo que en CPU/MPS es caro o inviable, usando la **MISMA receta ganadora**
del pool local para que los resultados sean comparables 1:1:

- **Confirmación multi-semilla** de los ganadores `AutoBiTCN` (FAD) y `BiTCN` (DFF) con HPO
  más amplio (`--num-samples 50-100`) — en CPU cada semilla con HPO tarda ~4-5 min; en GPU
  es barato correr 10 semillas y reportar media ± IC.
- **Frontier transformers** (Informer, Autoformer, FEDformer, PatchTST, iTransformer,
  TimesNet) entrenados **GLOBALES** — ¿baten al `AutoBiTCN` local (MASE 0.108 FAD)?

La receta (idéntica a `run_global_deep.py`): **global + diferencia + normalización por serie
+ HPO sin fuga**, hold-out de 24 meses a 1 paso (`cross_validation`).

## Por qué un entorno aparte
- `neuralforecast` exige `pandas<3`, incompatible con el `pandas==3.0.0` del pipeline
  principal → vive en su propio entorno (igual que el venv local `ante_nf`). El bundle pin-ea
  **`neuralforecast==3.1.9`**, la MISMA versión que `ante_nf`, para que la receta corra idéntica.
- Transformers/SSM rinden en GPU; en CPU/Apple Silicon son lentos o imposibles (Mamba = kernel CUDA).

## Pasos
1. Provisiona una instancia GPU (p.ej. AWS **g5.xlarge**, A10G 24 GB, Deep Learning AMI).
2. Copia el bundle y el panel:
   ```bash
   scp -r aws_gpu data/processed/visa_panel_long.parquet  ec2-user@<host>:~/run/
   ```
3. En la instancia:
   ```bash
   cd run/aws_gpu
   python -m venv venv && source venv/bin/activate
   pip install -r requirements.txt          # torch CUDA ya viene en la DL-AMI
   python train_gpu.py --selfcheck          # valida la reintegración (sin GPU)

   # (a) CONFIRMAR multi-semilla el ganador con HPO amplio:
   python train_gpu.py --panel ../visa_panel_long.parquet --table FAD --diff --local-scaler \
       --auto --models AutoBiTCN --num-samples 80 --seeds 1 2 3 4 5 6 7 8 9 10
   python train_gpu.py --panel ../visa_panel_long.parquet --table DFF --diff \
       --auto --models AutoBiTCN AutoTiDE --num-samples 50 --seeds 1 2 3 4 5

   # (b) FRONTIER pesado global (¿bate a AutoBiTCN 0.108?):
   python train_gpu.py --panel ../visa_panel_long.parquet --table FAD --diff --local-scaler \
       --models Informer Autoformer FEDformer PatchTST TimesNet --max-steps 2000
   ```
4. Copia los `reports/global_FAD_*.csv` / `global_DFF_*.csv` de vuelta al repo (`reports/`) y
   evalúa con las métricas del proyecto en el entorno principal:
   ```bash
   ante/bin/python aggregate_seeds.py --table FAD --prefix auto_s --model AutoBiTCN   # media ± IC
   ante/bin/python -c "from vp_model.eval_neuralforecast import eval_global_deep, global_summary; \
       print(global_summary(eval_global_deep('FAD')))"                                # ranking frontier
   ```

## Modelos
Frontier (`--models`): Informer, Autoformer, FEDformer, PatchTST, TimesNet, BiTCN, TiDE, NHITS,
iTransformer, TimeMixer (los 2 últimos multivariados — pueden fallar con series de distinta
longitud; el loop los aísla). Auto (`--auto`): AutoBiTCN, AutoTiDE, AutoNHITS, AutoPatchTST,
AutoInformer, AutoTimesNet.

**Mamba/S-Mamba**: NO viene en neuralforecast; requiere `pip install mamba-ssm causal-conv1d`
(build CUDA) + un wrapper de modelo propio. Marcado experimental.

**Chronos fine-tuning (LoRA)**: ver `chronos_lora.py` (compara contra el zero-shot 0.225 del pool local).

## Expectativa honesta (resultados locales ya obtenidos)
El veredicto local (CPU/MPS) ya es que **el deep entrenado con esta receta VENCE a la
parsimonia como modelo único: FAD `AutoBiTCN` 0.108 (vs ETS/Theta 0.118), DFF `BiTCN` 0.088
(vs SARIMA 0.100)** — confirmado multi-semilla en local (DFF 0.0895 ± 0.0006). La GPU sirve
para: (1) **endurecer** esa confirmación con más semillas y HPO más amplio; (2) ver si el
**frontier pesado** (transformers de horizonte largo, Mamba) supera al `AutoBiTCN`. NO se
asume que lo haga: con n=125-290 por serie la capacidad extra puede no ayudar. Reportar lo que
de verdad salga.

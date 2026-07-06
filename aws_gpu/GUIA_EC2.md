# Guía — correr el frontier/multi-semilla en una EC2 g5.xlarge

Guía paso a paso para disparar el bundle GPU en una instancia EC2 **g5.xlarge** (1× NVIDIA
A10G 24 GB). Es la vía **exploratoria** recomendada: interactiva, sin maquinaria de SageMaker,
y mi bundle corre tal cual. El cómputo pesado pasa en la nube; la **evaluación** (MASE/CRPS,
`aggregate_seeds.py`) la sigues haciendo en tu Mac con el entorno principal.

> **Costo de referencia:** g5.xlarge ≈ **\$1.006/hora** on-demand en `us-east-1` (us-east-1 es
> donde ya tienes el bucket S3). Un barrido típico (multi-semilla + frontier) son ~2–4 horas →
> **\$2–4**. Lo único que arruina esto es **olvidar apagar la instancia**: ver §7.

---

## 0. Prerrequisitos (una sola vez)

- **Cuota de GPU.** Las cuentas nuevas suelen traer 0 vCPU de "Running On-Demand G instances".
  Pídela en *Service Quotas → EC2 → "Running On-Demand G and VT instances"* y sube el límite a
  **≥ 4 vCPU** (g5.xlarge = 4 vCPU). La aprobación tarda de minutos a 1–2 días. Tu cuenta es
  `564141855321`.
- **Key pair** (par de llaves SSH): *EC2 → Key Pairs → Create*, formato `.pem`, guárdalo en
  `~/.ssh/` y `chmod 400 ~/.ssh/visapredict-gpu.pem`.
- **Región `us-east-1`** (misma del bucket; así jalas el panel desde S3 sin transferencia entre
  regiones).

## 1. Lanzar la instancia

En la consola EC2 → *Launch instance*:

- **Name:** `visapredict-gpu`
- **AMI:** busca **"Deep Learning OSS Nvidia Driver AMI GPU PyTorch"** (Ubuntu 22.04). Trae driver
  CUDA + PyTorch GPU listos. ⚠️ El **ID** de la AMI cambia por región y versión; no lo hardcodeo
  aquí — selecciónala por nombre en el buscador de AMIs.
- **Instance type:** `g5.xlarge`
- **Key pair:** el que creaste.
- **Network / Security group:** crea uno que permita **SSH (22) solo desde tu IP** (la consola
  ofrece "My IP"). No abras 22 a `0.0.0.0/0`.
- **Storage:** sube el volumen raíz a **100 GB gp3** (las AMIs de DL son grandes).
- **IAM instance profile (opcional pero cómodo):** un rol con `AmazonS3ReadOnlyAccess` para jalar
  el panel desde S3 sin copiar credenciales.

Lanza y espera a *Running* + *2/2 checks passed*. Anota la **IP pública**.

## 2. Conectarte y subir el bundle + el panel

```bash
# en tu Mac
export GPU=ubuntu@<IP_PUBLICA>
KEY=~/.ssh/visapredict-gpu.pem

# subir el bundle y el panel (el .parquet es regenerable con `make db`; está gitignored)
scp -i $KEY -r aws_gpu data/processed/visa_panel_long.parquet $GPU:~/run/
ssh -i $KEY $GPU
```

> **Alternativa al scp del panel:** si pusiste el rol IAM con acceso a S3, en la instancia puedes
> regenerar/obtener el panel desde tu bucket en lugar de subirlo. Pero el `.parquet` pesa poco,
> así que el `scp` directo es lo más simple.

## 3. Preparar el entorno (en la instancia)

```bash
cd ~/run/aws_gpu
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt            # torch CUDA ya viene en la AMI
python train_gpu.py --selfcheck            # valida la reintegración (sin GPU)
python -c "import torch; print('CUDA disponible:', torch.cuda.is_available())"   # debe decir True
```

## 4. Correr los experimentos

El panel quedó en `~/run/visa_panel_long.parquet` (un nivel arriba de `aws_gpu/`).

> **⚡ Atajo de UN comando (recomendado):** en vez de copiar los bloques de abajo, corre el
> orquestador que ya trae el bundle — hace las 4 fases (multi-semilla del ganador + frontier
> pesado, FAD y DFF) y **apaga la instancia sola** al terminar:
> ```bash
> tmux new -s gpu                 # sobrevive si se cae el SSH
> bash run_frontier.sh            # panel por defecto ../visa_panel_long.parquet
> # Ctrl-b d para soltar el tmux; reconecta con: tmux attach -t gpu
> ```
> Los bloques manuales de abajo quedan como referencia / para correr fases sueltas.

```bash
PANEL=../visa_panel_long.parquet

# (a) CONFIRMAR multi-semilla el ganador con HPO AMPLIO (lo que el CPU no alcanza):
python train_gpu.py --panel $PANEL --table FAD --local-scaler \
    --auto --models AutoBiTCN --num-samples 80 --seeds 1 2 3 4 5 6 7 8 9 10
python train_gpu.py --panel $PANEL --table DFF \
    --auto --models AutoBiTCN AutoTiDE --num-samples 50 --seeds 1 2 3 4 5

# (b) FRONTIER pesado global — ¿bate a AutoBiTCN 0.109 (FAD) / al campeón desplegado 0.100 (DFF)? (baseline vigente en key_facts)
python train_gpu.py --panel $PANEL --table FAD --local-scaler \
    --models Informer Autoformer FEDformer PatchTST TimesNet --max-steps 2000
python train_gpu.py --panel $PANEL --table DFF \
    --models Informer Autoformer FEDformer PatchTST TimesNet --max-steps 2000

# (c) Chronos: zero-shot (reproduce el 0.225) y, si quieres, fine-tune LoRA (ver chronos_lora.py)
python chronos_lora.py zeroshot --panel $PANEL --table FAD
```

Tip para no tener que vigilarlo: corre dentro de `tmux` (sobrevive si se cae el SSH) y, en la
**última** corrida del lote, agrega `--shutdown-on-done` para que la instancia se apague sola al
terminar (red de seguridad de costo):

```bash
tmux new -s gpu
# ... lanzas las corridas; en la última:
python train_gpu.py --panel $PANEL --table FAD --local-scaler \
    --models Informer Autoformer FEDformer PatchTST TimesNet --max-steps 2000 --shutdown-on-done
# Ctrl-b d  para soltar el tmux; reconecta con: tmux attach -t gpu
```

## 5. Bajar los resultados a tu Mac

```bash
# en tu Mac (comillas DOBLES: dejan expandir $GPU localmente y protegen el * para el shell remoto)
scp -i "$KEY" "$GPU:~/run/aws_gpu/reports/global_*.csv" reports/campaign/
scp -i "$KEY" "$GPU:~/run/aws_gpu/reports/chronos_*.csv" reports/campaign/
```

## 6. Evaluar localmente (entorno principal, como siempre)

```bash
cd ~/Documents/Anteproyecto/VisaPredictAI
# multi-semilla media ± IC del ganador:
ante/bin/python aggregate_seeds.py --table FAD --prefix auto_s --model AutoBiTCN
# ranking del frontier vs el listón:
ante/bin/python -c "from vp_model.eval_neuralforecast import eval_global_deep, global_summary; \
    print(global_summary(eval_global_deep('FAD')))"
```

Los CSV de resultados están **gitignored** (regenerables); no ensucian el repo. Si el frontier
o el HPO amplio bate a AutoBiTCN, actualizamos la tabla `tab:global_deep` y el veredicto del `.tex`.

## 7. ⚠️ APAGAR / TERMINAR la instancia (lo más importante)

La GPU cobra **por cada hora encendida**, corras o no. Al terminar:

- **Stop** (detener): conserva el disco, no cobra cómputo, sí cobra el EBS (~\$8/mes los 100 GB).
  Útil si vas a volver pronto.
- **Terminate** (terminar): borra todo, costo \$0. Úsalo cuando ya bajaste los resultados.

```bash
# desde tu Mac, con el AWS CLI configurado:
aws ec2 terminate-instances --instance-ids <INSTANCE_ID> --region us-east-1
```

Redes de seguridad recomendadas:
1. **`--shutdown-on-done`** en la última corrida (ya lo hace el script: `sudo shutdown -h +1`).
   Ojo: `shutdown -h` hace **stop**, no terminate — igual deja de cobrar cómputo, pero acuérdate
   de *terminar* después.
2. **Budget alarm:** *Billing → Budgets →* alerta a tu correo si el gasto del mes pasa de, p. ej.,
   \$10. Tarda 2 minutos y te cubre el "se me olvidó".

---

### Resumen de un vistazo
1. Cuota G + key pair → 2. Lanzar g5.xlarge (DL AMI, SG solo-tu-IP) → 3. `scp aws_gpu + panel` →
4. venv + `pip install` → 5. correr (tmux + `--shutdown-on-done`) → 6. `scp` CSVs de vuelta →
7. evaluar local → 8. **terminar la instancia**.

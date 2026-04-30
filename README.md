# UtilesIA

Servicio local en Python: **PDF → texto (PyMuPDF)** → **LLM** (API compatible OpenAI / LM Studio).

## Instalación

```powershell
cd C:\Users\Andres\Source\Python\UtilesIA
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Ejecutar

Desde **esta carpeta** (`UtilesIA`), no desde otro sitio:

```powershell
cd C:\Users\Andres\Source\Python\UtilesIA
.\.venv\Scripts\activate
python -m uvicorn app:app --host 0.0.0.0 --port 8787 --reload
```

O ejecuta **`run_utilesia.bat`** en esta carpeta.

Comprueba:

- `http://localhost:8787/health` → debe incluir `"build": "2026-04-29-process-json-v2"`
- `http://localhost:8787/version` → debe listar `post_paths` con `/process-json`

Si **POST /process-json** da **404** pero `/health` va bien, casi siempre hay **otro proceso viejo** en el puerto 8787 o uvicorn arrancó sin recargar el `app.py` nuevo: cierra todas las ventanas de uvicorn/Python, en PowerShell `Get-NetTCPConnection -LocalPort 8787` y mata el proceso, vuelve a arrancar desde la carpeta `UtilesIA`.

## Petición

`POST /process` — formulario multipart:

| Campo | Descripción |
|--------|-------------|
| `file` | PDF |
| `api_url` | Ej. `http://192.168.10.238:1234/v1/chat/completions` |
| `model_name` | Id del modelo en LM Studio |
| `context_length` | `n_ctx` del modelo (trunca el texto extraído si no cabe) |

Ejemplo con curl:

```powershell
curl -X POST "http://localhost:8787/process" `
  -F "file=@C:\ruta\presupuesto.pdf" `
  -F "api_url=http://192.168.10.238:1234/v1/chat/completions" `
  -F "model_name=qwen2.5-vl-7b-instruct" `
  -F "context_length=128000"
```

La respuesta JSON incluye `assistant_text` (contenido del modelo) y `raw_response`.

### JSON desde Business Central (`POST /process-json`)

Mismo flujo que `/process`, pero el body es JSON:

```json
{
  "pdf_base64": "...",
  "api_url": "http://IP:1234/v1/chat/completions",
  "model_name": "tu-modelo",
  "context_length": 128000
}
```

En **Información de la empresa** (BC) configurad **URL servicio UtilesIA** apuntando a la base (ej. `http://servidor:8787`).

## PDF escaneado

Si no hay texto seleccionable, la extracción queda vacía: hace falta OCR u otro flujo.

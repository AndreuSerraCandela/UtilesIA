"""
UtilesIA — Servicio local: PDF → texto → LLM (API compatible OpenAI / LM Studio).

Ejecutar: uvicorn app:app --host 0.0.0.0 --port 8787
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cambiad si comprobáis que el servidor cargó el código actual (útil si POST /process-json da 404).
APP_BUILD = "2026-05-01-plain-default-table-optin"

app = FastAPI(title="UtilesIA", description="Extracción PDF a texto y envío a LLM")


@app.middleware("http")
async def log_each_request(request, call_next):
    logger.info("%s %s", request.method, request.url.path)
    return await call_next(request)


@app.on_event("startup")
async def log_registered_routes():
    lines: list[str] = []
    for r in app.routes:
        p = getattr(r, "path", None)
        m = getattr(r, "methods", None)
        if p and m:
            lines.append(f"{sorted(m)} {p}")
    logger.info("UtilesIA build=%s rutas: %s", APP_BUILD, sorted(lines))

TOKEN_OVERHEAD = 2800

_SYSTEM_RULES_COST_QTY = """Campos precio/descuento (deben cuadrar con el presupuesto: cantidad × precio neto ≈ importe línea):
- **PROHIBIDO** usar la columna **Artículo**, **Nº**, **Ref.**, **Código** o referencia numérica del producto (ej. 138443, 103224) como **directUnitCost**. Esos son códigos, no euros. Pon ese código en **vendorItemNo** si lo ves.
- **directUnitCost** debe salir SOLO de columnas de **importe**: preferible **Neto** (precio unitario neto). Jamás tomes el número más a la izquierda de la fila pensando que es precio.
- **Opción preferida:** **directUnitCost** = valor de la columna **Neto** (unitario tras dto). **lineDiscountPct** = 0.
- **Opción válida:** **directUnitCost** = columna **Precio** (bruto) y **lineDiscountPct** = dto numérico (ej. 40 para "40,00" si es porcentaje). Si eliges esta opción, NO copies el Neto en directUnitCost sin aplicar la lógica del dto.
- Ejemplo correcto: Artículo 138443, Precio 265, Desc 40, **Neto 159** → directUnitCost **159.0**, lineDiscountPct **0**, vendorItemNo **138443** (si procede). INCORRECTO: directUnitCost 138443 o 265 sin dto cuando hay Neto 159.
- Si en **Dto** solo pone texto tipo "Oferta" sin porcentaje, **directUnitCost** = **Neto**, **lineDiscountPct** = 0.
- NO pongas el porcentaje de dto en directUnitCost. NO uses **Importe** total de línea (cantidad × neto) como precio unitario.
- Orden habitual de columnas: **Artículo | Descripción | Cantidad | UM | Precio | Desc | Neto** — respétalo al leer números.
- Si el PDF tiene **Código** y **Precio Unit.** (sin columna Neto): **vendorItemNo** = código; **directUnitCost** = Precio Unit. (número unitario, no el Importe total de línea). Comprueba que cantidad × directUnitCost ≈ Importe (tolerancia redondeo).
- Si falta dato: directUnitCost 0, lineDiscountPct 0.
Incluye siempre **lineDiscountPct** en cada objeto (0 si usas precio neto en directUnitCost).
OBLIGATORIO en numeros JSON: solo PUNTO como separador decimal (ej. 10.7762, 3.06). PROHIBIDO la coma en numeros.
Cantidad con punto si es decimal (ej. 2.0)."""

_SYSTEM_RULES_TABLE_ROWS = """Solo si el usuario incluyó seccion "### TABLAS" con filas separadas por " | ":
- Usa esas filas como fuente principal; cada fila → un objeto JSON (salvo cabeceras o totales).
- Incluye todas las lineas de detalle (codigo vacio incluido). Coherencia por fila: no mezcles precios entre lineas.
- Codigo → vendorItemNo; Precio Unit. → directUnitCost (nunca al revés)."""

_SYSTEM_RULES_DESCRIPTION_QUALITY = """Calidad de **description**:
- Copia la descripcion de CADA linea tal como aparece en el PDF para esa linea. Esta PROHIBIDO reutilizar la misma descripcion en todos los objetos si en el documento las descripciones son distintas (LAMPARA, BOMBILLA, CABLE, etc. deben ser textos distintos).
- Si una descripcion es muy larga, recortala pero sin sustituirla por la de otra linea."""

_SYSTEM_PROMPT_CORE_NO_HISTORY = (
    """Eres un extractor de datos de presupuestos PDF (obras, suministros, servicios).
Debes responder ÚNICAMENTE con un array JSON válido, sin texto antes ni después.
Formato de cada elemento:
{"description":"texto","quantity":0,"directUnitCost":0,"lineDiscountPct":0,"vendorItemNo":""}

"""
    + _SYSTEM_RULES_COST_QTY
    + "\n"
    + _SYSTEM_RULES_DESCRIPTION_QUALITY
)

_SYSTEM_PROMPT_CORE_WITH_HISTORY = (
    """Eres un extractor de datos de presupuestos PDF (obras, suministros, servicios).
Debes responder ÚNICAMENTE con un array JSON válido, sin texto antes ni después.
Formato de cada elemento:
{"description":"texto","quantity":0,"directUnitCost":0,"lineDiscountPct":0,"vendorItemNo":"","lineType":"","no":""}

Si hay historico de compras del proveedor: lineType debe ser uno de Item, G_L_Account, Resource, Fixed_Asset, Charge_Item.
El campo no debe copiar EXACTAMENTE un codigo que exista en el historico (producto o cuenta G/L). Si no hay match claro, lineType y no como "". NO inventes codigos.

"""
    + _SYSTEM_RULES_COST_QTY
    + "\n"
    + _SYSTEM_RULES_DESCRIPTION_QUALITY
)

USER_PROMPT_HISTORY_BLOCK = """
--- HISTORICO DE COMPRAS RECIENTES (mismo proveedor) ---
Cada linea tiene formato: tipo|codigo|descripcion|fecha_publicacion
tipos posibles en el historico: Item, G_L_Account, Resource, Fixed_Asset, Charge_Item
Para cada linea del presupuesto, elige lineType y no copiando EXACTAMENTE tipo y codigo de una fila del historico cuando encaje; si no encaja, lineType y no vacios.

{history}
--- FIN HISTORICO ---
"""

USER_PROMPT_TEMPLATE = """Extrae lineas de compra del siguiente texto del PDF (suele incluir cabeceras de tabla y lineas con codigo, descripcion, cantidades e importes).
Respeta el orden del documento: codigo de articulo → vendorItemNo; precio unitario/neto → directUnitCost (nunca el codigo como precio).
Devuelve SOLO el array JSON de lineas de detalle (excluye subtotales/IVA globales).

--- TEXTO DEL DOCUMENTO ---
{text}
--- FIN ---"""


def build_system_prompt(has_purchase_history: bool, has_structured_tables: bool = False) -> str:
    core = _SYSTEM_PROMPT_CORE_WITH_HISTORY if has_purchase_history else _SYSTEM_PROMPT_CORE_NO_HISTORY
    if has_structured_tables:
        return core + "\n\n" + _SYSTEM_RULES_TABLE_ROWS
    return core


def build_user_prompt(extracted_pdf_text: str, purchase_history_lines: list[str]) -> str:
    doc = USER_PROMPT_TEMPLATE.format(text=extracted_pdf_text)
    if not purchase_history_lines:
        return doc
    history = "\n".join(line.strip() for line in purchase_history_lines if line and str(line).strip())
    if not history:
        return doc
    return doc + USER_PROMPT_HISTORY_BLOCK.format(history=history)


class ProcessJsonBody(BaseModel):
    """Cuerpo JSON para clientes que no envían multipart (p. ej. Business Central)."""

    pdf_base64: str = Field(..., description="PDF codificado en Base64")
    api_url: str = Field(..., description="URL chat completions del LLM")
    model_name: str
    context_length: int = Field(128000, description="n_ctx del modelo")
    purchase_history_lines: list[str] = Field(
        default_factory=list,
        description="Lineas de contexto de compras previas, ej. Item|180012|TORNILLOS|2025-03-01",
    )


def _table_to_pipe_rows(table: Any) -> str:
    """Convierte una tabla PyMuPDF en líneas texto|celda2|... sin depender de pandas."""
    try:
        rows = table.extract()
    except Exception:
        return ""
    if not rows:
        return ""
    out: list[str] = []
    for row in rows:
        cells: list[str] = []
        for cell in row:
            if cell is None:
                s = ""
            else:
                s = str(cell).replace("|", "/").replace("\n", " ").strip()
            cells.append(s)
        out.append(" | ".join(cells))
    return "\n".join(out)


def _extract_tables_sections_from_pdf(doc: fitz.Document) -> str:
    sections: list[str] = []
    for page_index, page in enumerate(doc):
        try:
            tf = page.find_tables()
        except Exception as exc:
            logger.debug("find_tables pagina %s: %s", page_index + 1, exc)
            continue
        tables = getattr(tf, "tables", None) or []
        for t_i, table in enumerate(tables):
            try:
                block = _table_to_pipe_rows(table)
            except Exception as exc:
                logger.debug("extract tabla p%s #%s: %s", page_index + 1, t_i + 1, exc)
                continue
            if block.strip():
                sections.append(f"--- Pagina {page_index + 1}, tabla {t_i + 1} ---\n{block}")
    blob = "\n\n".join(sections).strip()
    if blob:
        logger.info("PDF: detectadas %s bloques de tabla en %s paginas", len(sections), len(doc))
    return blob


def extract_text_from_pdf(pdf_bytes: bytes) -> tuple[str, bool]:
    """
    Texto para el LLM y si se antepondrán tablas detectadas por PyMuPDF.
    Por defecto SOLO texto plano (mas estable). Tablas: UTILESIA_ENABLE_TABLE_EXTRACTION=1.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        plain_parts: list[str] = []
        for page in doc:
            plain_parts.append(page.get_text("text"))

        plain = "\n\n".join(p.strip() for p in plain_parts if p and p.strip()).strip()

        tables_on = os.environ.get("UTILESIA_ENABLE_TABLE_EXTRACTION", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not tables_on:
            return plain, False

        tables_blob = _extract_tables_sections_from_pdf(doc)
        if tables_blob:
            return (
                "### TABLAS (filas con separador | ; contrastar con texto plano si algo cuadra mal)\n\n"
                + tables_blob
                + "\n\n### TEXTO PLANO DEL PDF\n\n"
                + plain
            ), True
        return plain, False
    finally:
        doc.close()


def _ocr_disabled() -> bool:
    return os.environ.get("UTILESIA_DISABLE_OCR", "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_tesseract_executable() -> str | None:
    """Ruta al binario tesseract: env TESSERACT_CMD, PATH, o instalacion tipica en Windows."""
    cmd = os.environ.get("TESSERACT_CMD", "").strip()
    if cmd:
        p = Path(cmd)
        if p.is_file():
            return str(p.resolve())
        return cmd

    found = shutil.which("tesseract")
    if found:
        return found

    if os.name == "nt":
        for root in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            r"C:\Program Files",
            r"C:\Program Files (x86)",
        ):
            if not root:
                continue
            candidate = Path(root) / "Tesseract-OCR" / "tesseract.exe"
            if candidate.is_file():
                logger.info("Tesseract encontrado: %s", candidate)
                return str(candidate.resolve())

    return None


def extract_text_from_pdf_ocr(pdf_bytes: bytes) -> str:
    """
    Rasteriza cada página (PyMuPDF) y lee texto con Tesseract.
    Requiere el binario `tesseract` en PATH o la variable TESSERACT_CMD (ruta al .exe en Windows).
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "Faltan dependencias OCR: ejecute pip install pillow pytesseract"
        ) from e

    resolved = _resolve_tesseract_executable()
    if resolved:
        pytesseract.pytesseract.tesseract_cmd = resolved

    try:
        from pytesseract import TesseractNotFoundError
    except ImportError:
        TesseractNotFoundError = OSError  # type: ignore[misc,assignment]

    scale = float(os.environ.get("UTILESIA_OCR_SCALE", "2"))
    lang = os.environ.get("UTILESIA_OCR_LANG", "spa+eng")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts: list[str] = []
    mat = fitz.Matrix(scale, scale)
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            try:
                parts.append(pytesseract.image_to_string(img, lang=lang))
            except TesseractNotFoundError as e:
                raise RuntimeError(
                    "Tesseract OCR no esta instalado o no se encuentra el ejecutable. "
                    "Windows: instale desde https://github.com/UB-Mannheim/tesseract/wiki "
                    "(ruta tipica: C:\\Program Files\\Tesseract-OCR\\tesseract.exe). "
                    "Reinicie Uvicorn tras instalar o defina TESSERACT_CMD con la ruta al .exe."
                ) from e
    finally:
        doc.close()

    return "\n\n".join(p.strip() for p in parts if p and p.strip()).strip()


def extract_text_from_pdf_with_optional_ocr(pdf_bytes: bytes) -> tuple[str, bool, bool]:
    """
    Devuelve (texto, usó_ocr, incluye_bloques_tabla_para_prompt).
    OCR no combina con detección de tablas (solo texto OCR).
    """
    text, used_tables = extract_text_from_pdf(pdf_bytes)
    if text.strip():
        return text, False, used_tables
    if _ocr_disabled():
        return "", False, False
    ocr_text = extract_text_from_pdf_ocr(pdf_bytes)
    return ocr_text, True, False


def truncate_for_context(
    text: str,
    context_length: int,
    reserve_chars: int = 0,
) -> tuple[str, bool]:
    if context_length <= TOKEN_OVERHEAD:
        raise ValueError(f"context_length debe ser mayor que {TOKEN_OVERHEAD}")

    budget_tokens = context_length - TOKEN_OVERHEAD
    max_chars = max(2048, budget_tokens * 3 - max(0, reserve_chars))

    if len(text) <= max_chars:
        return text, False

    truncated = text[:max_chars]
    logger.warning(
        "Texto truncado: %s caracteres → %s (context_length=%s, reserve_chars=%s)",
        len(text),
        max_chars,
        context_length,
        reserve_chars,
    )
    return truncated, True


async def call_openai_compatible(
    api_url: str,
    model_name: str,
    system_prompt: str,
    user_content: str,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    payload = {
        "model": model_name,
        "temperature": 0,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            api_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    body_text = r.text
    try:
        body_json = r.json()
    except json.JSONDecodeError:
        body_json = {"raw": body_text}

    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "El servidor LLM respondió con error",
                "status_code": r.status_code,
                "body": body_json if isinstance(body_json, dict) else body_text,
            },
        )

    return body_json


def strip_llm_markdown_fence(text: str) -> str:
    """Quita envoltorio ``` o ```json que a veces envuelve el array."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def repair_european_commas_in_numeric_fields(text: str) -> str:
    """
    El LLM suele escribir importes como 3,06; JSON valido exige 3.06.
    Solo toca valores tras claves conocidas para no romper strings.
    """
    keys = ("directUnitCost", "quantity", "lineDiscountPct", "line_discount_pct")
    out = text
    for key in keys:
        pattern = rf'("{re.escape(key)}"\s*:\s*)(-?\d+),(\d+)'

        def repl(m: re.Match[str]) -> str:
            return f"{m.group(1)}{m.group(2)}.{m.group(3)}"

        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


def normalize_assistant_json_text(raw: str) -> tuple[str, bool, bool]:
    """
    Devuelve (texto_para_cliente, json_parseable, se_aplico_reparacion).
    Si tras reparaciones es JSON valido, re-serializa con json.dumps para formato estable.
    """
    raw_stripped = raw.strip()
    stripped = strip_llm_markdown_fence(raw)
    repaired = repair_european_commas_in_numeric_fields(stripped)
    repaired_flag = raw_stripped != stripped or stripped != repaired

    for candidate in (repaired, stripped, raw_stripped):
        try:
            parsed = json.loads(candidate)
            canon = json.dumps(parsed, ensure_ascii=False)
            return canon, True, repaired_flag
        except json.JSONDecodeError:
            continue

    return repaired, False, repaired_flag


def parse_assistant_content(response_json: dict[str, Any]) -> str:
    try:
        choices = response_json.get("choices") or []
        if not choices:
            return json.dumps(response_json, ensure_ascii=False)
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content is None:
            return json.dumps(response_json, ensure_ascii=False)
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return json.dumps(response_json, ensure_ascii=False)


async def run_pipeline(
    pdf_bytes: bytes,
    api_url: str,
    model_name: str,
    context_length: int,
    purchase_history_lines: list[str] | None = None,
) -> dict[str, Any]:
    if not pdf_bytes:
        raise HTTPException(400, "PDF vacío")

    try:
        extracted, ocr_used, pdf_table_sections_in_prompt = extract_text_from_pdf_with_optional_ocr(
            pdf_bytes
        )
    except RuntimeError as e:
        logger.warning("OCR no disponible o falló la configuración: %s", e)
        raise HTTPException(
            422,
            str(e),
        ) from e
    except Exception as e:
        logger.exception("Error extrayendo PDF")
        raise HTTPException(400, f"No se pudo leer el PDF: {e}") from e

    if not extracted.strip():
        if ocr_used:
            hint = (
                "Se aplicó OCR al PDF pero no se reconoció texto útil "
                "(calidad baja, idioma o datos spa.traineddata faltantes: instale paquetes de idioma en Tesseract). "
                "Pruebe UTILESIA_OCR_LANG=eng o suba la resolución con UTILESIA_OCR_SCALE=3."
            )
        elif _ocr_disabled():
            hint = (
                "No hay texto seleccionable en el PDF y UTILESIA_DISABLE_OCR está activo; "
                "quite esa variable para permitir OCR con Tesseract."
            )
        else:
            hint = (
                "No se extrajo texto del PDF (¿documento escaneado como imagen?). "
                "Instale Tesseract OCR y reinicie el servicio, o defina TESSERACT_CMD. "
                "Windows: https://github.com/UB-Mannheim/tesseract/wiki — "
                "Si no desea OCR automático: UTILESIA_DISABLE_OCR=1."
            )
        raise HTTPException(422, hint)

    hist_lines = list(purchase_history_lines or [])
    hist_joined = "\n".join(x.strip() for x in hist_lines if x and str(x).strip())
    reserve_chars = len(hist_joined) + (900 if hist_joined else 0)

    try:
        text_for_llm, was_truncated = truncate_for_context(
            extracted,
            context_length,
            reserve_chars=reserve_chars,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    has_history = bool(hist_joined)
    system_prompt = build_system_prompt(has_history, pdf_table_sections_in_prompt)
    user_content = build_user_prompt(text_for_llm, hist_lines)

    try:
        raw = await call_openai_compatible(api_url, model_name, system_prompt, user_content)
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(502, f"No se pudo conectar al LLM: {e}") from e

    assistant_raw = parse_assistant_content(raw)
    assistant_text, assistant_json_ok, assistant_json_repaired = normalize_assistant_json_text(
        assistant_raw
    )
    if not assistant_json_ok:
        logger.warning(
            "Respuesta del LLM no es JSON parseable tras reparar comas/cercado markdown "
            "(primeros 240 chars): %s",
            assistant_text[:240],
        )

    return {
        "success": True,
        "assistant_text": assistant_text,
        "assistant_json_ok": assistant_json_ok,
        "assistant_json_repaired": assistant_json_repaired,
        "extracted_char_count": len(extracted),
        "sent_char_count": len(user_content),
        "truncated": was_truncated,
        "context_length": context_length,
        "raw_response": raw,
        "ocr_used": ocr_used,
        "pdf_table_sections_in_prompt": pdf_table_sections_in_prompt,
        "purchase_history_line_count": len(hist_lines),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "UtilesIA", "build": APP_BUILD}


@app.get("/version")
async def version():
    """Comprueba que este proceso tiene POST /process-json (si da 404 en BC pero /version muestra build viejo, reiniciad uvicorn)."""
    posts: list[str] = []
    for r in app.routes:
        m = getattr(r, "methods", None)
        p = getattr(r, "path", "")
        if m and "POST" in m and p:
            posts.append(p)
    return {"build": APP_BUILD, "post_paths": sorted(posts)}


@app.post("/process-json")
async def process_json(body: ProcessJsonBody):
    """
    Misma lógica que /process pero recibe el PDF en Base64 (útil para Business Central HttpClient).
    """
    try:
        raw_bytes = base64.b64decode(body.pdf_base64)
    except Exception as e:
        raise HTTPException(400, f"pdf_base64 inválido: {e}") from e

    result = await run_pipeline(
        raw_bytes,
        body.api_url.strip(),
        body.model_name.strip(),
        body.context_length,
        body.purchase_history_lines,
    )
    return JSONResponse(result)


@app.post("/process/process-json", include_in_schema=False)
async def process_json_legacy_double_path(body: ProcessJsonBody):
    """Compatibilidad si la URL base terminaba en /process y se concatenaba /process-json por error."""
    try:
        raw_bytes = base64.b64decode(body.pdf_base64)
    except Exception as e:
        raise HTTPException(400, f"pdf_base64 inválido: {e}") from e

    result = await run_pipeline(
        raw_bytes,
        body.api_url.strip(),
        body.model_name.strip(),
        body.context_length,
        body.purchase_history_lines,
    )
    return JSONResponse(result)


@app.post("/process")
async def process(
    file: UploadFile = File(..., description="PDF del presupuesto"),
    api_url: str = Form(..., description="URL chat completions, ej. http://host:1234/v1/chat/completions"),
    model_name: str = Form(..., description="Nombre del modelo en LM Studio"),
    context_length: int = Form(
        128000,
        description="n_ctx / tamaño de contexto del modelo (para truncar texto si hace falta)",
    ),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Se espera un fichero .pdf")

    pdf_bytes = await file.read()
    result = await run_pipeline(pdf_bytes, api_url, model_name, context_length, [])
    return JSONResponse(result)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8787, reload=True)

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
APP_BUILD = "2026-05-05-layout-dedupe-plain"

# Ruta del fichero utilesia.env aplicado al arrancar (solo lectura para /settings).
_UTILESIA_ENV_SOURCE: str | None = None


def _apply_utilesia_env_file(path: Path) -> None:
    """Carga KEY=VALUE en el proceso. No sobrescribe variables ya definidas en el entorno."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("No se pudo leer %s: %s", path, e)
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            continue
        key, val = s.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def load_utilesia_env_from_disk() -> None:
    """
    Opcional: fichero utilesia.env junto a app.py (copiar desde utilesia.env.example).
    Las variables de entorno del sistema/servicio tienen prioridad (setdefault).
    """
    global _UTILESIA_ENV_SOURCE
    candidate = Path(__file__).resolve().parent / "utilesia.env"
    if not candidate.is_file():
        return
    _apply_utilesia_env_file(candidate)
    _UTILESIA_ENV_SOURCE = str(candidate)
    logger.info("UtilesIA: variables opcionales cargadas desde %s", candidate.name)


def env_flag_enabled(name: str, *, default: bool = False) -> bool:
    """True si la variable es 1/true/yes/on (insensible a mayúsculas)."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


load_utilesia_env_from_disk()

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


def resolve_llm_max_completion_tokens() -> int:
    """
    Tope de tokens de salida en chat completions. Sin esto, LM Studio / algunos backends
    permiten generaciones muy largas y modelos pequeños pueden divagar miles de tokens.

    UTILESIA_LLM_MAX_TOKENS — por defecto 4096 (suficiente para muchas líneas en JSON).
    """
    raw = os.environ.get("UTILESIA_LLM_MAX_TOKENS", "4096").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 4096
    return max(512, min(n, 120000))

_SYSTEM_RULES_EXTENDED_COLUMNS = """La salida es **solo** el array JSON: sin explicacion, sin repetir el enunciado, sin markdown; debe terminar en **]** y detenerse.

Columnas numéricas del PDF (rellena cada una con el valor **literal** de la tabla; **0** si esa columna no existe en el documento para esa línea):
- **listUnitPrice**: precio unitario **bruto** / lista / antes del dto (columnas tipo Precio, P.Unit., Precio Unit.).
- **netUnitPrice**: precio unitario **neto** tras dto (Neto, P. Neto).
- **lineAmount**: **importe total de línea** (Importe, Total línea). Es cantidad × neto habitualmente; NO lo uses como precio unitario.
- **documentDiscountPct**: porcentaje de descuento **tal como en el PDF** (Dto, Desc, %); ej. 52 para "52,00". **0** si no hay número o solo texto ("Oferta").

**vendorItemNo**: código/ref. artículo del PDF. **PROHIBIDO** usar código numérico de artículo como precio en listUnitPrice, netUnitPrice o directUnitCost.

**Compatibilidad pedidos (directUnitCost + lineDiscountPct)** — deben ser coherentes entre sí (el cliente puede ignorar columnas extendidas y usar solo estos dos):
- Si **netUnitPrice** > 0: **directUnitCost** = netUnitPrice y **lineDiscountPct** = **0** (el dto del PDF queda reflejado en documentDiscountPct y en la diferencia lista→neto).
- Si **netUnitPrice** es 0 pero **listUnitPrice** > 0: **directUnitCost** = listUnitPrice y **lineDiscountPct** = documentDiscountPct.
- Si solo hay un precio unitario sin columnas separadas: ponlo en listUnitPrice o netUnitPrice según el encabezado del PDF; directUnitCost igual al precio que corresponda aplicar en pedido; documentDiscountPct y lineDiscountPct coherentes con la fila.

Ejemplo: Precio 265, Desc 40, Neto 159 → listUnitPrice **265**, documentDiscountPct **40**, netUnitPrice **159**, lineAmount según importe línea, directUnitCost **159**, lineDiscountPct **0**.

OBLIGATORIO en numeros JSON: solo **PUNTO** decimal (ej. 37.92). PROHIBIDO la coma en numeros.
**quantity** con punto si es decimal (ej. 2.0).
Incluye **siempre** en cada objeto las claves listUnitPrice, netUnitPrice, lineAmount, documentDiscountPct, directUnitCost, lineDiscountPct (usa 0 donde no aplique).
El array JSON debe estar COMPLETO (no truncar la respuesta)."""

_SYSTEM_RULES_TABLE_ROWS = """Si hay seccion "### LINEAS_DETALLE_ALINEADAS_POR_COORDENADAS" y/o "### TABLAS_PYMUPDF":
- Prioriza LINEAS_DETALLE (filas codigo | descripcion | cantidad | precio_unitario | importe_linea) como fuente por fila; el texto plano suele tener numeros en orden incorrecto o **repetir** el mismo listado de articulos.
- Si LINEAS_DETALLE incluye **DETALLE_CANONICO: N filas**, el array JSON debe tener **exactamente N** objetos, en el **mismo orden**, sin filas inventadas ni duplicar el bloque de lineas otra vez.
- Una descripcion que en el PDF ocupa **varias lineas** es **una sola fila** de detalle: en LINEAS_DETALLE ya suele ir el texto unido; genera **un solo** objeto JSON (no uno por linea de texto del PDF).
- **listUnitPrice** = precio unitario de la fila si la tabla es precio bruto; si la cabecera indica precio neto, usa **netUnitPrice**. **lineAmount** = importe_linea. **documentDiscountPct** si aparece dto en esa fila o en cabecera clara.
- Comprueba cantidad * precio unitario ≈ importe_linea (tolerancia redondeo).
- Para "### TABLAS_PYMUPDF": mismas reglas por fila.
- Codigo → vendorItemNo; nunca como precio."""

_SYSTEM_RULES_DESCRIPTION_QUALITY = """Calidad de **description**:
- Copia la descripcion de CADA linea tal como aparece en el PDF para esa linea. Esta PROHIBIDO reutilizar la misma descripcion en todos los objetos si en el documento las descripciones son distintas (LAMPARA, BOMBILLA, CABLE, etc. deben ser textos distintos).
- Si una descripcion es muy larga, recortala pero sin sustituirla por la de otra linea.
- Si LINEAS_DETALLE ya une una descripcion multilinea en una sola fila codigo|descripcion|..., usa ese texto unido en **description** (no partas en dos objetos JSON)."""

_SYSTEM_PROMPT_CORE_NO_HISTORY = (
    """Eres un extractor de datos de presupuestos PDF (obras, suministros, servicios).
Debes responder ÚNICAMENTE con un array JSON válido, sin texto antes ni después.
Formato de cada elemento:
{"description":"","quantity":0,"vendorItemNo":"","listUnitPrice":0,"netUnitPrice":0,"lineAmount":0,"documentDiscountPct":0,"directUnitCost":0,"lineDiscountPct":0}

"""
    + _SYSTEM_RULES_EXTENDED_COLUMNS
    + "\n"
    + _SYSTEM_RULES_DESCRIPTION_QUALITY
)

_SYSTEM_PROMPT_CORE_WITH_HISTORY = (
    """Eres un extractor de datos de presupuestos PDF (obras, suministros, servicios).
Debes responder ÚNICAMENTE con un array JSON válido, sin texto antes ni después.
Formato de cada elemento:
{"description":"","quantity":0,"vendorItemNo":"","listUnitPrice":0,"netUnitPrice":0,"lineAmount":0,"documentDiscountPct":0,"directUnitCost":0,"lineDiscountPct":0,"lineType":"","no":""}

Clasificacion contable (solo si el prompt incluye bloque HISTORICO):
- **lineType** debe ser exactamente uno de: Item, G_L_Account, Resource, Fixed_Asset, Charge_Item (mismos nombres que en el historico).
- **vendorItemNo** es el codigo/ref. del proveedor en el PDF (catalogo del proveedor). NO es el numero de producto Item en Business Central salvo que el historico demuestre que ese codigo se usa como Item.
- **no** es el codigo BC del tipo elegido: numero de producto si lineType=Item, numero de cuenta si lineType=G_L_Account, etc. Debe coincidir **literalmente** con el campo codigo de **alguna** fila del historico del mismo lineType (sin inventar numeros). No uses 1, 2, 3… como **no** salvo que aparezcan como codigo Item en el historico.
- Si una fila del PDF no encaja con una linea concreta del historico (descripcion/categoria distinta), usa la **moda** del historico: el par (lineType, codigo) mas repetido en las lineas recientes. Si casi todo es G_L_Account con la misma cuenta (ej. 622000010 o 602000003), las nuevas lineas de compra generica deben ir a esa cuenta salvo que una fila del historico sea un match claro de Item.
- Si el historico esta vacio o es ilegible, deja **lineType** y **no** como cadena vacia "".

"""
    + _SYSTEM_RULES_EXTENDED_COLUMNS
    + "\n"
    + _SYSTEM_RULES_DESCRIPTION_QUALITY
)

USER_PROMPT_HISTORY_BLOCK = """
--- HISTORICO DE COMPRAS RECIENTES (mismo proveedor) ---
Cada linea: tipo|codigo|descripcion|fecha_publicacion
tipos permitidos en tipo: Item, G_L_Account, Resource, Fixed_Asset, Charge_Item
Usa este historico para rellenar lineType y no en cada linea del PDF (reglas en el system). El codigo del PDF va solo en vendorItemNo.

{history}
--- FIN HISTORICO ---
"""

USER_PROMPT_TEMPLATE = """Extrae lineas de compra del siguiente texto del PDF (suele incluir cabeceras de tabla y lineas con codigo, descripcion, cantidades, precios, dto, netos e importes).
Respeta el orden del documento: codigo → vendorItemNo; copia columnas en listUnitPrice, netUnitPrice, lineAmount y documentDiscountPct; directUnitCost/lineDiscountPct coherentes con esas columnas (nunca el codigo como precio).
Si aparece LINEAS_DETALLE con DETALLE_CANONICO, no dupliques lineas ni re-leas el mismo catalogo dos veces (el texto plano suele repetir filas tras descripciones largas).
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


def log_llm_user_prompt_payload(
    user_content: str,
    *,
    extracted_full_len: int,
    text_for_llm_len: int,
    pdf_truncated: bool,
) -> None:
    """
    Registra el mensaje 'user' enviado al LLM (plantilla + texto del PDF + histórico si hay).

    Por defecto DESACTIVADO (evita volcar PDFs en logs).

    UTILESIA_LOG_LLM_USER_PROMPT=1 — activar (también true/yes/on).
    UTILESIA_LOG_LLM_PROMPT_MAX_CHARS — recorte del log (por defecto 12000); 0 = sin recorte.
    """
    if not env_flag_enabled("UTILESIA_LOG_LLM_USER_PROMPT", default=False):
        return

    raw_max = os.environ.get("UTILESIA_LOG_LLM_PROMPT_MAX_CHARS", "12000").strip()
    try:
        max_log_chars = int(raw_max)
    except ValueError:
        max_log_chars = 12000

    logger.info(
        "LLM mensaje usuario: total_chars=%s pdf_extraido_total=%s pdf_en_prompt=%s pdf_truncado_contexto=%s",
        len(user_content),
        extracted_full_len,
        text_for_llm_len,
        pdf_truncated,
    )

    if max_log_chars == 0:
        logger.info("LLM mensaje usuario (completo):\n%s", user_content)
        return

    if len(user_content) <= max_log_chars:
        logger.info("LLM mensaje usuario:\n%s", user_content)
        return

    logger.info(
        "LLM mensaje usuario (recorte log %s/%s chars):\n%s\n... [fin recorte log; UTILESIA_LOG_LLM_PROMPT_MAX_CHARS=0 para completo]",
        max_log_chars,
        len(user_content),
        user_content[:max_log_chars],
    )


class ProcessJsonBody(BaseModel):
    """Cuerpo JSON para clientes que no envían multipart (p. ej. Business Central)."""

    pdf_base64: str = Field(..., description="PDF codificado en Base64")
    api_url: str = Field(..., description="URL chat completions del LLM")
    model_name: str
    context_length: int = Field(128000, description="n_ctx del modelo")
    purchase_history_lines: list[str] = Field(
        default_factory=list,
        description=(
            "Compras previas mismo proveedor: tipo|codigo|descripcion|fecha (tipo en ingles BC: "
            "Item, G_L_Account, …). Ej. G_L_Account|622000010|Compras mercaderías|2025-03-01"
        ),
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


_SPANISH_DECIMAL_TOKEN_RE = re.compile(r"^\d+,\d+$")


def _cluster_pdf_words_into_lines(words: list[Any], y_tol: float = 6.0) -> list[list[Any]]:
    buckets: dict[int, list[Any]] = {}
    for w in words:
        mid_y = (float(w[1]) + float(w[3])) / 2.0
        key = int(round(mid_y / y_tol))
        buckets.setdefault(key, []).append(w)
    return [sorted(buckets[k], key=lambda x: float(x[0])) for k in sorted(buckets.keys())]


def _split_vendor_code_and_desc_tokens(left_tokens: list[str]) -> tuple[str, str]:
    if not left_tokens:
        return "", ""
    first = left_tokens[0]
    if first.isdigit() and len(first) >= 5:
        return first, " ".join(left_tokens[1:]).strip()
    return "", " ".join(left_tokens).strip()


def _count_layout_detail_rows(layout_blob: str) -> int:
    """Cuenta filas codigo|descripcion|cantidad|... en el bloque LINEAS_DETALLE (sin cabeceras marcadoras)."""
    n = 0
    for raw in layout_blob.splitlines():
        s = raw.strip()
        if not s:
            continue
        if "codigo | descripcion" in s:
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 5:
            n += 1
    return n


def _extract_factura_taller_lines_from_words(doc: fitz.Document) -> str:
    """
    Facturas taller / albaran donde get_text('text') desordena columnas.
    Reconstruye filas usando posiciones X/Y de get_text('words').
    """
    out_rows: list[str] = []
    x_split = float(os.environ.get("UTILESIA_LAYOUT_X_SPLIT", "350"))

    for page_idx, page in enumerate(doc):
        page_text = page.get_text("text")
        if "Importe" not in page_text or "Cantidad" not in page_text:
            continue

        words = page.get_text("words")
        if not words:
            continue

        lines = _cluster_pdf_words_into_lines(words, y_tol=6.0)
        seen_header = False
        desc_carry: list[str] = []

        for line_words in lines:
            sorted_w = sorted(line_words, key=lambda w: float(w[0]))
            joined = " ".join(str(w[4]).strip() for w in sorted_w if str(w[4]).strip())
            if not joined:
                continue

            # Cabecera tabla (encoding PDF puede romper "Codigo"/"Descripcion")
            if (
                "Importe" in joined
                and "Cantidad" in joined
                and ("Precio" in joined or "Unit" in joined)
                and len(joined) < 160
            ):
                seen_header = True
                out_rows.append(
                    f"(Pagina {page_idx + 1}) codigo | descripcion | cantidad | precio_unitario | importe_linea"
                )
                continue

            if not seen_header:
                continue

            if "OBSERVACIONES" in joined or joined.startswith("PEDIDO"):
                break
            if "Mano Obra" in joined and "Recambios" in joined:
                break

            nums_right: list[str] = []
            left_tokens: list[str] = []
            for w in sorted_w:
                t = str(w[4]).strip()
                if not t:
                    continue
                x = float(w[0])
                if x >= x_split:
                    if _SPANISH_DECIMAL_TOKEN_RE.match(t):
                        nums_right.append(t)
                else:
                    left_tokens.append(t)

            if len(nums_right) >= 3:
                qty, p_unit, imp_ln = nums_right[-3], nums_right[-2], nums_right[-1]
                code, desc_line = _split_vendor_code_and_desc_tokens(left_tokens)
                prefix = " ".join(desc_carry).strip()
                desc_carry = []
                desc = (prefix + " " + desc_line).strip()
                out_rows.append(f"{code} | {desc} | {qty} | {p_unit} | {imp_ln}")
            elif seen_header and left_tokens:
                frag = " ".join(left_tokens).strip()
                noise = ("Documento", "Fecha", "Marca", "Modelo", "N.I.F", "Recepcionista", "Cod.Cli")
                if frag and not any(frag.startswith(p) for p in noise):
                    desc_carry.append(frag)

    if len(out_rows) <= 1:
        return ""
    logger.info("PDF: reconstruidas %s filas por coordenadas (factura taller)", len(out_rows) - 1)
    return "\n".join(out_rows)


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
    Texto para el LLM y si hay bloque estructurado suplementario (coordenadas y/o tablas PyMuPDF).
    Siempre intenta reconstruir lineas por palabras en facturas taller (sin variable env).
    Tablas find_tables: UTILESIA_ENABLE_TABLE_EXTRACTION=1.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        plain_parts: list[str] = []
        for page in doc:
            plain_parts.append(page.get_text("text"))

        plain = "\n\n".join(p.strip() for p in plain_parts if p and p.strip()).strip()

        supplements: list[str] = []
        structured = False
        layout_detail_row_count = 0

        if os.environ.get("UTILESIA_DISABLE_WORD_LAYOUT", "").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            layout_lines = _extract_factura_taller_lines_from_words(doc)
            if layout_lines:
                layout_detail_row_count = _count_layout_detail_rows(layout_lines)
                if layout_detail_row_count >= 1:
                    layout_header = (
                        "### LINEAS_DETALLE_ALINEADAS_POR_COORDENADAS\n"
                        f"(DETALLE_CANONICO: {layout_detail_row_count} filas codigo|descripcion|cantidad|precio|importe. "
                        "El JSON debe tener exactamente ese numero de objetos en el mismo orden. "
                        "Descripcion multilinea en el PDF: aqui ya fusionada en una fila; "
                        "prohibido duplicar filas ni volver a importar el listado desde texto plano.)\n\n"
                    )
                else:
                    layout_header = (
                        "### LINEAS_DETALLE_ALINEADAS_POR_COORDENADAS\n"
                        "(Reconstruccion por posicion en el PDF; priorizar sobre texto plano para codigo, "
                        "descripcion, cantidad, precio unitario e importe de linea)\n\n"
                    )
                supplements.append(layout_header + layout_lines)
                structured = True

        tables_on = os.environ.get("UTILESIA_ENABLE_TABLE_EXTRACTION", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if tables_on:
            tables_blob = _extract_tables_sections_from_pdf(doc)
            if tables_blob:
                supplements.append("### TABLAS_PYMUPDF\n\n" + tables_blob)
                structured = True

        suppress_plain = env_flag_enabled(
            "UTILESIA_SUPPRESS_PLAIN_WITH_LAYOUT", default=True
        ) and (layout_detail_row_count >= 1)

        if supplements:
            joined = "\n\n".join(supplements)
            if suppress_plain:
                logger.info(
                    "PDF: omitiendo TEXTO_PLANO (UTILESIA_SUPPRESS_PLAIN_WITH_LAYOUT; %s filas LINEAS_DETALLE)",
                    layout_detail_row_count,
                )
                return joined, structured
            return (
                joined
                + "\n\n### TEXTO_PLANO_ORIGINAL_PDF\n\n"
                + plain
            ), structured

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
    max_out = resolve_llm_max_completion_tokens()
    payload = {
        "model": model_name,
        "temperature": 0,
        "stream": False,
        "max_tokens": max_out,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    logger.info(
        "LLM request: max_tokens=%s (ajustar UTILESIA_LLM_MAX_TOKENS si trunca JSON)",
        max_out,
    )

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


_NUMERIC_JSON_KEYS_COMMA_REPAIR = (
    "directUnitCost",
    "quantity",
    "lineDiscountPct",
    "line_discount_pct",
    "listUnitPrice",
    "netUnitPrice",
    "lineAmount",
    "documentDiscountPct",
)


def repair_european_commas_in_numeric_fields(text: str) -> str:
    """
    El LLM suele escribir importes como 3,06; JSON valido exige 3.06.
    Solo toca valores tras claves conocidas para no romper strings.
    """
    keys = _NUMERIC_JSON_KEYS_COMMA_REPAIR
    out = text
    for key in keys:
        pattern = rf'("{re.escape(key)}"\s*:\s*)(-?\d+),(\d+)'

        def repl(m: re.Match[str]) -> str:
            return f"{m.group(1)}{m.group(2)}.{m.group(3)}"

        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


def repair_json_trailing_commas(text: str) -> str:
    """Quita comas finales antes de ] o } (JSON estricto)."""
    t = text
    for _ in range(12):
        nxt = re.sub(r",\s*]", "]", t)
        nxt = re.sub(r",\s*}", "}", nxt)
        if nxt == t:
            break
        t = nxt
    return t


def salvage_partial_json_array_raw_decode(text: str) -> str | None:
    """
    Recupera objetos JSON completos cuando la respuesta se corto a mitad del array
    (p.ej. vendorItemNo sin cerrar comilla).
    """
    s = text.strip()
    if not s.startswith("["):
        return None
    dec = json.JSONDecoder()
    idx = 1
    items: list[Any] = []
    while idx < len(s):
        while idx < len(s) and s[idx] in " \n\r\t,":
            idx += 1
        if idx >= len(s) or s[idx] == "]":
            break
        try:
            obj, end = dec.raw_decode(s, idx)
        except json.JSONDecodeError:
            break
        items.append(obj)
        idx = end
    if not items:
        return None
    logger.warning(
        "JSON del LLM incompleto: se devolvieron %s objetos validos (resto truncado)",
        len(items),
    )
    return json.dumps(items, ensure_ascii=False)


def salvage_truncated_json_array(text: str) -> str | None:
    """
    Si el modelo corto la respuesta a mitad de cadena/objeto, intenta cerrar hasta el ultimo } completo.
    """
    base = text.strip()
    if not base.startswith("["):
        return None
    for cut in range(len(base), max(2, len(base) // 4), -1):
        chunk = base[:cut].rstrip().rstrip(",")
        while chunk and chunk[-1] not in "}]":
            chunk = chunk[:-1].rstrip().rstrip(",")
        if not chunk.endswith("}"):
            continue
        open_curly = chunk.count("{") - chunk.count("}")
        open_sq = chunk.count("[") - chunk.count("]")
        trial = chunk + ("}" * max(0, open_curly)) + ("]" * max(0, open_sq))
        trial = repair_json_trailing_commas(trial)
        try:
            parsed = json.loads(trial)
            if isinstance(parsed, list) and parsed:
                return json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError:
            continue
    return None


_NUMERIC_KEYS_CANONICALIZE = frozenset(
    {
        "quantity",
        "directunitcost",
        "linediscountpct",
        "line_discount_pct",
        "listunitprice",
        "netunitprice",
        "lineamount",
        "documentdiscountpct",
    }
)


def _parse_optional_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.strip().replace(" ", "")
        if not t:
            return None
        tnorm = t.replace(",", ".") if "," in t and t.count(",") == 1 else t
        try:
            return float(tnorm)
        except ValueError:
            return None
    return None


def derive_bc_fields_from_extended_columns(data: Any) -> Any:
    """
    Ajusta directUnitCost y lineDiscountPct desde columnas extendidas del PDF.

    UTILESIA_DERIVE_BC_FROM_EXTENDED=0 — no modificar (solo lo que devolvió el LLM).
    Por defecto: si netUnitPrice > 0 → directUnitCost = neto, lineDiscountPct = 0;
    si no hay neto pero listUnitPrice > 0 → directUnitCost = lista, lineDiscountPct desde documentDiscountPct o lineDiscountPct previo.
    """
    off = os.environ.get("UTILESIA_DERIVE_BC_FROM_EXTENDED", "1").strip().lower()
    if off in ("0", "false", "no", "off"):
        return data
    if isinstance(data, list):
        return [derive_bc_fields_from_extended_columns(x) for x in data]
    if not isinstance(data, dict):
        return data

    obj = data
    net = _parse_optional_float(obj.get("netUnitPrice"))
    lst = _parse_optional_float(obj.get("listUnitPrice"))
    doc_explicit = obj.get("documentDiscountPct")
    has_doc_key = "documentDiscountPct" in obj
    doc_num = _parse_optional_float(doc_explicit) if has_doc_key else None
    fallback_dto = _parse_optional_float(obj.get("lineDiscountPct"))

    if net is not None and net > 0:
        obj["directUnitCost"] = round(net, 5)
        obj["lineDiscountPct"] = 0
        if not has_doc_key and fallback_dto is not None:
            obj["documentDiscountPct"] = round(fallback_dto, 5)
    elif lst is not None and lst > 0:
        dto_apply = round(doc_num if doc_num is not None else (fallback_dto or 0), 5)
        obj["directUnitCost"] = round(lst, 5)
        obj["lineDiscountPct"] = dto_apply
        if not has_doc_key and fallback_dto is not None:
            obj["documentDiscountPct"] = round(fallback_dto, 5)

    return obj


def canonicalize_line_items_for_bc(data: Any) -> Any:
    """Redondea numeros para evitar formatos raros; BC tolera mejor pocas cifras decimales."""
    if isinstance(data, list):
        return [canonicalize_line_items_for_bc(x) for x in data]
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            lk = str(k).lower()
            if lk in _NUMERIC_KEYS_CANONICALIZE:
                if isinstance(v, bool):
                    out[k] = v
                elif v is None:
                    out[k] = v
                elif isinstance(v, (int, float)):
                    if lk == "quantity":
                        q = float(v)
                        out[k] = int(q) if abs(q - round(q)) < 1e-9 else round(q, 5)
                    else:
                        out[k] = round(float(v), 5)
                elif isinstance(v, str):
                    t = v.strip().replace(" ", "")
                    tnorm = t.replace(",", ".") if "," in t and t.count(",") == 1 else t
                    try:
                        fv = float(tnorm)
                        if lk == "quantity":
                            out[k] = int(fv) if abs(fv - round(fv)) < 1e-9 else round(fv, 5)
                        else:
                            out[k] = round(fv, 5)
                    except ValueError:
                        out[k] = v
                else:
                    out[k] = v
            else:
                out[k] = v
        return out
    return data


def normalize_assistant_json_text(raw: str) -> tuple[str, bool, bool]:
    """
    Devuelve (texto_para_cliente, json_parseable, se_aplico_reparacion).
    Si tras reparaciones es JSON valido, re-serializa con json.dumps para formato estable.
    """
    raw_stripped = raw.strip()
    stripped = strip_llm_markdown_fence(raw)
    repaired = repair_european_commas_in_numeric_fields(stripped)
    repaired = repair_json_trailing_commas(repaired)
    repaired_flag = raw_stripped != stripped or stripped != repaired

    candidates: list[str] = [repaired, stripped, raw_stripped]
    partial_ok = salvage_partial_json_array_raw_decode(repaired)
    if partial_ok:
        candidates.insert(0, partial_ok)
        repaired_flag = True
    salvaged = salvage_truncated_json_array(repaired)
    if salvaged:
        candidates.insert(0, salvaged)
        repaired_flag = True

    for candidate in candidates:
        if not candidate.strip():
            continue
        try:
            parsed = json.loads(candidate)
            parsed = canonicalize_line_items_for_bc(parsed)
            parsed = derive_bc_fields_from_extended_columns(parsed)
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

    log_llm_user_prompt_payload(
        user_content,
        extracted_full_len=len(extracted),
        text_for_llm_len=len(text_for_llm),
        pdf_truncated=was_truncated,
    )

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


@app.get("/settings")
async def settings():
    """Configuración efectiva (sin secretos). utilesia.env se documenta en utilesia.env.example."""
    return {
        "build": APP_BUILD,
        "env_file_loaded": _UTILESIA_ENV_SOURCE,
        "log_llm_user_prompt": env_flag_enabled("UTILESIA_LOG_LLM_USER_PROMPT", default=False),
        "log_llm_prompt_max_chars": os.environ.get("UTILESIA_LOG_LLM_PROMPT_MAX_CHARS", "12000"),
        "suppress_plain_pdf_with_layout": env_flag_enabled(
            "UTILESIA_SUPPRESS_PLAIN_WITH_LAYOUT", default=True
        ),
    }


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

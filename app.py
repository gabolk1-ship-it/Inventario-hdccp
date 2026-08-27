import os
import uuid
import json
import io
import base64

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

# ─── Credenciales Google ───────────────────────────────────────────────────────
# En Vercel se carga desde la variable de entorno GOOGLE_CREDENTIALS (JSON string)
# En local se lee desde credentials.json
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_google_creds() -> Credentials:
    env_creds = os.environ.get("GOOGLE_CREDENTIALS")
    if env_creds:
        info = json.loads(env_creds)
    else:
        creds_file = os.path.join(os.path.dirname(__file__), "credentials.json")
        with open(creds_file) as f:
            info = json.load(f)
    return Credentials.from_service_account_info(info, scopes=SCOPES)

# ─── Google Sheets ─────────────────────────────────────────────────────────────
SHEET_ID  = "1fzxVf2FcpwP1ROTZw7fto1Aoa1jXlwkwdSRjI5O5mls"
SHEET_GID = 768695945

def get_sheet():
    creds  = get_google_creds()
    client = gspread.authorize(creds)
    sh     = client.open_by_key(SHEET_ID)
    for ws in sh.worksheets():
        if ws.id == SHEET_GID:
            return ws
    return sh.get_worksheet(0)

# ─── Google Drive ──────────────────────────────────────────────────────────────
DRIVE_FOLDER_ID = None   # Si quieres una carpeta específica en Drive, pon el ID aquí

def subir_a_drive(contenido: bytes, nombre: str, mime: str = "image/jpeg") -> str:
    """
    Sube un archivo a Google Drive y retorna la URL pública de visualización.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload

    creds   = get_google_creds()
    service = build("drive", "v3", credentials=creds)

    file_meta = {"name": nombre}
    if DRIVE_FOLDER_ID:
        file_meta["parents"] = [DRIVE_FOLDER_ID]

    media = MediaIoBaseUpload(io.BytesIO(contenido), mimetype=mime, resumable=False)
    archivo = service.files().create(
        body=file_meta,
        media_body=media,
        fields="id"
    ).execute()

    file_id = archivo.get("id")

    # Hacer público
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()

    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w400"


async def procesar_imagen(archivo: Optional[UploadFile]) -> Optional[str]:
    """Lee el UploadFile, lo sube a Drive y retorna la URL."""
    if not archivo or not archivo.filename:
        return None
    try:
        contenido = await archivo.read()
        ext  = os.path.splitext(archivo.filename)[1].lower() or ".jpg"
        mime = "image/png" if ext == ".png" else "image/jpeg"
        nombre = f"inventario_{uuid.uuid4().hex}{ext}"
        return subir_a_drive(contenido, nombre, mime)
    except Exception as e:
        print(f"Error subiendo imagen a Drive: {e}")
        return None

# ─── Helpers Sheets ────────────────────────────────────────────────────────────
def col_idx(headers: list, name: str) -> Optional[int]:
    try:    return headers.index(name)
    except: return None

def tipo_prefix(tipo: str) -> str:
    t = tipo.lower()
    for k in ["cpu", "monitor", "teclado", "mouse", "impresora"]:
        if k in t:
            return k.capitalize()
    if "escritorio" in t or "torre" in t: return "CPU"
    if "portatil" in t or "portátil" in t: return "CPU"
    if "uno" in t: return "CPU"
    return "CPU"

FOTO_COLS = {
    "CPU":       ("Foto General CPU",       "Foto Etiqueta CPU",       "OCR CPU"),
    "Monitor":   ("Foto General Monitor",   "Foto Etiqueta Monitor",   "OCR Monitor"),
    "Teclado":   ("Foto General Teclado",   "Foto Etiqueta Teclado",   "OCR Teclado"),
    "Mouse":     ("Foto General Mouse",     "Foto Etiqueta Mouse",     "OCR Mouse"),
    "Impresora": ("Foto General Impresora", "Foto Etiqueta Impresora", "OCR Impresora"),
}

def escribir_sheets(data: dict) -> None:
    ws      = get_sheet()
    headers = ws.row_values(1)
    fila_num = data.get("fila")

    # Si se especificó una fila existente (> 1), actualizamos esa fila sin borrar datos
    if fila_num and isinstance(fila_num, int) and fila_num > 1:
        valores_actuales = ws.row_values(fila_num)
        fila = list(valores_actuales) + [""] * max(0, len(headers) - len(valores_actuales))

        def sc(col, val):
            i = col_idx(headers, col)
            if i is not None and val:
                fila[i] = str(val)

        if data.get("codigo_bien"):     sc("Código del bien IESS", data["codigo_bien"])
        if data.get("codigo_auxiliar"): sc("Código Auxiliar (Unnamed: 1)", data["codigo_auxiliar"])
        if data.get("marca"):           sc("Marca del equipo", data["marca"])
        if data.get("modelo"):          sc("Modelo del equipo", data["modelo"])
        if data.get("serie"):           sc("Serie del equipo", data["serie"])
        if data.get("ciudad"):          sc("Ciudad", data["ciudad"])
        if data.get("estado"):          sc("Operativo", "SI" if data["estado"] == "Bueno" else "NO")

        prefix = tipo_prefix(data.get("tipo", ""))
        if prefix in FOTO_COLS:
            c_eq, c_et, c_ocr = FOTO_COLS[prefix]
            if data.get("foto_equipo"):   sc(c_eq, data["foto_equipo"])
            if data.get("foto_etiqueta"): sc(c_et, data["foto_etiqueta"])
            if data.get("ocr_raw"):       sc(c_ocr, data["ocr_raw"])

        ws.update([fila], f"A{fila_num}", value_input_option="USER_ENTERED")
        return

    # Si es registro nuevo, creamos fila y append_row
    fila = [""] * len(headers)

    def sc(col, val):
        i = col_idx(headers, col)
        if i is not None and val:
            fila[i] = str(val)

    sc("Código del bien IESS",                  data.get("codigo_bien", ""))
    sc("Código Auxiliar (Unnamed: 1)",           data.get("codigo_auxiliar", ""))
    sc("Tipo de bien",                           data.get("tipo", ""))
    sc("Marca del equipo",                       data.get("marca", ""))
    sc("Modelo del equipo",                      data.get("modelo", ""))
    sc("Serie del equipo",                       data.get("serie", ""))
    sc("Sistema Operativo",                      data.get("sistema_operativo", ""))
    sc("Tamaño del Disco",                       data.get("disco_tam", ""))
    sc("Unidad Disco",                           data.get("disco_unidad", ""))
    sc("Memoria RAM",                            data.get("ram", ""))
    sc("Procesador",                             data.get("procesador", ""))
    sc("Provincia",                              data.get("provincia", "PICHINCHA"))
    sc("Ciudad",                                 data.get("ciudad", "QUITO"))
    sc("Dependencia/Edificio",                   data.get("dependencia", ""))
    sc("Ubicación / Area Funcional",             data.get("ubicacion", ""))
    sc("Cédula Custodio",                        data.get("cedula_custodio", ""))
    sc("Nombre del Custodio",                    data.get("responsable", ""))
    sc("Tiene Garantía",                         data.get("garantia", "NO"))
    sc("Proveedor del equipo",                   data.get("proveedor", ""))
    sc("Operativo",                              "SI" if data.get("estado","Bueno") == "Bueno" else "NO")
    sc("Dirección IP",                           data.get("ip", ""))
    sc("MAC Address",                            data.get("mac", ""))
    sc("Nombre completo del equipo (hostname)",  data.get("hostname", ""))
    sc("ID_Unico",                               data.get("id_unico", str(uuid.uuid4())))

    # Marcar casilla del tipo
    tipo_str = data.get("tipo", "").upper()
    if "TODO EN UNO" in tipo_str or "AIO" in tipo_str:
        sc("COMPUTADOR TODO EN UNO", "1")
    elif "PORTÁTIL" in tipo_str or "PORTATIL" in tipo_str:
        sc("COMPUTADOR PORTÁTIL", "1")
    elif "ESCRITORIO" in tipo_str or "CPU" in tipo_str:
        sc("COMPUTADOR DE ESCRITORIO", "1")

    prefix = tipo_prefix(data.get("tipo", ""))
    if prefix in FOTO_COLS:
        c_eq, c_et, c_ocr = FOTO_COLS[prefix]
        sc(c_eq,  data.get("foto_equipo", ""))
        sc(c_et,  data.get("foto_etiqueta", ""))
        sc(c_ocr, data.get("ocr_raw", ""))

    ws.append_row(fila, value_input_option="USER_ENTERED")


def leer_sheets() -> list:
    ws      = get_sheet()
    records = ws.get_all_records(head=1)
    result  = []
    for i, r in enumerate(records, start=2):
        def s(k):
            val = r.get(k, "")
            if val is None:
                return ""
            return str(val).strip()

        tipo = s("Tipo de bien")
        if not tipo:
            continue
        result.append({
            "fila":          i,
            "id_unico":      s("ID_Unico"),
            "codigo_bien":   s("Código del bien IESS"),
            "codigo_aux":    s("Código Auxiliar (Unnamed: 1)"),
            "tipo":          tipo,
            "marca":         s("Marca del equipo"),
            "modelo":        s("Modelo del equipo"),
            "serie":         s("Serie del equipo"),
            "sistema_op":    s("Sistema Operativo"),
            "ram":           s("Memoria RAM"),
            "procesador":    s("Procesador"),
            "dependencia":   s("Dependencia/Edificio"),
            "ubicacion":     s("Ubicación / Area Funcional"),
            "responsable":   s("Nombre del Custodio"),
            "cedula":        s("Cédula Custodio"),
            "operativo":     s("Operativo"),
            "ip":            s("Dirección IP"),
            "mac":           s("MAC Address"),
            "hostname":      s("Nombre completo del equipo (hostname)"),
            "foto_cpu":      s("Foto General CPU"),
            "foto_monitor":  s("Foto General Monitor"),
            "foto_teclado":  s("Foto General Teclado"),
            "foto_mouse":    s("Foto General Mouse"),
            "foto_impresora":s("Foto General Impresora"),
        })
    return result

# ─── App FastAPI ───────────────────────────────────────────────────────────────
app = FastAPI(title="Inventario IESS", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Archivos estáticos (solo en local; en Vercel se sirven directo)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    index = os.path.join(static_dir, "index.html")
    return FileResponse(
        index,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.get("/recopilar_info.bat", include_in_schema=False)
def download_bat():
    bat_path = os.path.join(os.path.dirname(__file__), "recopilar_info.bat")
    return FileResponse(
        bat_path,
        media_type="application/octet-stream",
        filename="recopilar_info.bat",
        headers={"Content-Disposition": 'attachment; filename="recopilar_info.bat"'}
    )


@app.post("/api/inventario")
async def crear_registro(
    tipo:              str  = Form(...),
    marca:             str  = Form(""),
    modelo:            str  = Form(""),
    serie:             str  = Form(""),
    ubicacion:         str  = Form(""),
    responsable:       str  = Form(""),
    cedula_custodio:   str  = Form(""),
    dependencia:       str  = Form(""),
    sistema_operativo: str  = Form(""),
    ram:               str  = Form(""),
    procesador:        str  = Form(""),
    ip:                str  = Form(""),
    mac:               str  = Form(""),
    hostname:          str  = Form(""),
    codigo_bien:       str  = Form(""),
    codigo_auxiliar:   str  = Form(""),
    ciudad:            str  = Form("QUITO"),
    estado:            str  = Form("Bueno"),
    ocr_raw:           str  = Form(""),
    fila:              Optional[int] = Form(None),
    foto_etiqueta: Optional[UploadFile] = File(None),
    foto_auxiliar: Optional[UploadFile] = File(None),
    foto_equipo:   Optional[UploadFile] = File(None),
):
    # Subir imágenes a Google Drive
    url_etiqueta = await procesar_imagen(foto_etiqueta)
    url_auxiliar = await procesar_imagen(foto_auxiliar)
    url_equipo   = await procesar_imagen(foto_equipo)
    id_unico     = str(uuid.uuid4())

    # Combinar URLs de fotos de etiquetas si ambas existen
    urls_et = [u for u in [url_etiqueta, url_auxiliar] if u]
    foto_etiqueta_final = "\n".join(urls_et) if urls_et else ""

    data = dict(
        id_unico=id_unico, codigo_bien=codigo_bien, codigo_auxiliar=codigo_auxiliar,
        tipo=tipo, marca=marca, modelo=modelo, serie=serie,
        sistema_operativo=sistema_operativo, ram=ram, procesador=procesador,
        dependencia=dependencia, ubicacion=ubicacion, ciudad=ciudad,
        cedula_custodio=cedula_custodio, responsable=responsable,
        ip=ip, mac=mac, hostname=hostname, estado=estado,
        foto_equipo=url_equipo or "",
        foto_etiqueta=foto_etiqueta_final,
        ocr_raw=ocr_raw,
        fila=fila,
    )

    try:
        escribir_sheets(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error Google Sheets: {e}")

    msg = f"✅ Datos y fotos de {tipo} actualizados en fila {fila}" if fila else f"✅ {tipo} guardado correctamente en Google Sheets"
    return {
        "ok": True,
        "id_unico": id_unico,
        "foto_equipo": url_equipo,
        "foto_etiqueta": url_etiqueta,
        "foto_auxiliar": url_auxiliar,
        "message": msg
    }


@app.get("/api/test", summary="Diagnóstico de conexión")
def test_conexion():
    import os, json
    resultado = {}
    # 1 — ¿Existe la variable de entorno?
    env_creds = os.environ.get("GOOGLE_CREDENTIALS")
    resultado["env_var_presente"] = bool(env_creds)
    if env_creds:
        try:
            info = json.loads(env_creds)
            resultado["client_email"]   = info.get("client_email", "no encontrado")
            resultado["project_id"]     = info.get("project_id", "no encontrado")
            resultado["creds_json_ok"]  = True
        except Exception as e:
            resultado["creds_json_ok"]  = False
            resultado["json_error"]     = str(e)
    resultado["gemini_key_presente"] = bool(os.environ.get("GEMINI_API_KEY"))
    gem_key = os.environ.get("GEMINI_API_KEY", "")
    if gem_key:
        try:
            import requests
            r_mod = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={gem_key.strip()}", timeout=8)
            if r_mod.status_code == 200:
                models_list = [m.get("name") for m in r_mod.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                resultado["gemini_modelos_disponibles"] = models_list
            else:
                resultado["gemini_error"] = f"{r_mod.status_code} - {r_mod.text[:200]}"
        except Exception as ex_g:
            resultado["gemini_error"] = str(ex_g)

    # 2 — ¿Puede conectar con Sheets?
    try:
        ws = get_sheet()
        resultado["sheets_conectado"] = True
        resultado["hoja_titulo"]      = ws.title
        resultado["filas"]            = ws.row_count
    except Exception as e:
        resultado["sheets_conectado"] = False
        resultado["sheets_error"]     = str(e)

    # 3 — ¿Puede subir a Drive?
    try:
        url_d = subir_a_drive(b"test", "test.txt", "text/plain")
        resultado["drive_ok"] = True
        resultado["drive_url"] = url_d
    except Exception as ex_d:
        resultado["drive_ok"] = False
        resultado["drive_error"] = str(ex_d)

    return resultado


@app.post("/api/ia-ocr", summary="Agente IA de Visión (Gemini Flash)")
async def ia_ocr(
    imagen: UploadFile = File(...),
    tipo_bien: Optional[str] = Form(None),
    gemini_key: Optional[str] = Form(None),
    modo_etiqueta: Optional[str] = Form(None)
):
    """
    Agente inteligente de reconocimiento visual de etiquetas técnicas IESS.
    Utiliza Gemini 3.5 Flash para extraer Código IESS, Código Auxiliar,
    Marca, Modelo, Número de Serie, MAC y Tipo de bien con alta precisión.
    """
    import requests
    key = gemini_key or os.environ.get("GEMINI_API_KEY")
    if not key or not key.strip():
        return {
            "success": False,
            "necesita_key": True,
            "mensaje": "Se requiere GEMINI_API_KEY para activar el Agente de Visión IA."
        }

    try:
        contenido = await imagen.read()
        b64_img = base64.b64encode(contenido).decode("utf-8")
        mime = imagen.content_type or "image/jpeg"

        guia_modo = ""
        if modo_etiqueta == "aux":
            guia_modo = """
ATENCIÓN PRIORITARIA: Esta imagen es específicamente de la ETIQUETA DEL CÓDIGO AUXILIAR / CÓDIGO DE BARRAS.
Busca y extrae prioritariamente la secuencia numérica de 10 a 14 dígitos (por ejemplo: 27038980000661, 27004610003867, etc.) y asígnala al campo "codigo_auxiliar".
"""
        elif modo_etiqueta == "bien":
            guia_modo = """
ATENCIÓN PRIORITARIA: Esta imagen es de la PLACA / ETIQUETA INSTITUCIONAL DEL CÓDIGO DEL BIEN IESS.
Busca y extrae prioritariamente el código del bien (ej: IM-0511, EI-0141, etc.) y colócalo en "codigo_bien". Si aparecen marca, modelo o serie, extráelos.
"""

        prompt = f"""Eres un perito técnico experto en inventario de TI y activos fijos del IESS (Instituto Ecuatoriano de Seguridad Social).
Analiza detalladamente esta fotografía de una etiqueta técnica, código de barras, placa de activo fijo o chasis ({tipo_bien or 'equipo informático / periférico'}).
{guia_modo}
Tu objetivo es leer y estructurar con extrema exactitud los identificadores técnicos.
Responde ÚNICAMENTE un JSON válido sin texto adicional con esta estructura exacta:
{{
  "codigo_bien": "",
  "codigo_auxiliar": "",
  "marca": "",
  "modelo": "",
  "serie": "",
  "tipo": "",
  "mac": "",
  "observaciones": "",
  "texto_leido": ""
}}

Reglas de extracción:
- "codigo_bien": Código institucional IESS visible (ej: IM-0511, EI-0141, etc.). Si no aparece, deja "".
- "codigo_auxiliar": Código numérico auxiliar o código de barras de 10-14 dígitos (ej: 27004610003867). Si no aparece, deja "".
- "marca": Marca del fabricante (HP, Dell, Lenovo, Logitech, Epson, Samsung, etc.).
- "modelo": Modelo comercial o número de modelo exacto.
- "serie": Número de serie, Serial Number, S/N, Service Tag o N/S. Cuidado crítico: no confundir O con 0, I con 1, 8 con B.
- "tipo": Tipo de bien si es evidente (COMPUTADOR DE ESCRITORIO, COMPUTADOR TODO EN UNO, COMPUTADOR PORTÁTIL, MONITOR, TECLADO, MOUSE, IMPRESORA). Si es All-in-One, pon "COMPUTADOR TODO EN UNO".
- "mac": Dirección MAC física de red si está impresa (ej: 00:1A:2B:3C:4D:5E).
- "texto_leido": Todo el texto relevante que logres transcribir de la etiqueta.
"""

        modelos = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
        errores_modelos = []

        for mod in modelos:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{mod}:generateContent?key={key.strip()}"
                payload = {
                    "contents": [{
                        "parts": [
                            {"text": prompt},
                            {
                                "inline_data": {
                                    "mime_type": mime,
                                    "data": b64_img
                                }
                            }
                        ]
                    }],
                    "generationConfig": {
                        "temperature": 0.1,
                        "response_mime_type": "application/json"
                    }
                }
                resp = requests.post(url, json=payload, timeout=25)
                if resp.status_code == 200:
                    data_json = resp.json()
                    cands = data_json.get("candidates", [])
                    if cands:
                        raw_text = cands[0]["content"]["parts"][0]["text"]
                        parsed = json.loads(raw_text)
                        return {
                            "success": True,
                            "motor": f"Agente IA ({mod})",
                            "datos": parsed
                        }
                else:
                    errores_modelos.append(f"{mod}: {resp.status_code} - {resp.text[:150]}")
            except Exception as ex_m:
                errores_modelos.append(f"{mod}: {str(ex_m)}")
                continue

        return {
            "success": False,
            "error": f"Error conectando con Gemini Vision: {' | '.join(errores_modelos)}"
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Error procesando imagen con IA: {str(e)}"
        }



@app.get("/api/inventario")
def listar():
    try:
        datos = leer_sheets()
        return JSONResponse(
            content=datos,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo Sheets: {e}")


@app.delete("/api/inventario/{id_unico}")
def eliminar(id_unico: str):
    try:
        ws      = get_sheet()
        headers = ws.row_values(1)
        idx     = col_idx(headers, "ID_Unico")
        if idx is not None:
            vals = ws.col_values(idx + 1)
            for i, v in enumerate(vals):
                if v == id_unico:
                    ws.delete_rows(i + 1)
                    return {"ok": True, "message": f"Registro {id_unico} eliminado"}
        return {"ok": False, "message": "No encontrado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Local dev ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)

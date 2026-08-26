import os
import uuid
import json
import io
import base64

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
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
    fila    = [""] * len(headers)

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
        tipo = r.get("Tipo de bien", "").strip()
        if not tipo:
            continue
        result.append({
            "fila":          i,
            "id_unico":      r.get("ID_Unico", ""),
            "codigo_bien":   r.get("Código del bien IESS", ""),
            "codigo_aux":    r.get("Código Auxiliar (Unnamed: 1)", ""),
            "tipo":          tipo,
            "marca":         r.get("Marca del equipo", ""),
            "modelo":        r.get("Modelo del equipo", ""),
            "serie":         r.get("Serie del equipo", ""),
            "sistema_op":    r.get("Sistema Operativo", ""),
            "ram":           r.get("Memoria RAM", ""),
            "procesador":    r.get("Procesador", ""),
            "dependencia":   r.get("Dependencia/Edificio", ""),
            "ubicacion":     r.get("Ubicación / Area Funcional", ""),
            "responsable":   r.get("Nombre del Custodio", ""),
            "cedula":        r.get("Cédula Custodio", ""),
            "operativo":     r.get("Operativo", ""),
            "ip":            r.get("Dirección IP", ""),
            "mac":           r.get("MAC Address", ""),
            "hostname":      r.get("Nombre completo del equipo (hostname)", ""),
            "foto_cpu":      r.get("Foto General CPU", ""),
            "foto_monitor":  r.get("Foto General Monitor", ""),
            "foto_teclado":  r.get("Foto General Teclado", ""),
            "foto_mouse":    r.get("Foto General Mouse", ""),
            "foto_impresora":r.get("Foto General Impresora", ""),
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
    return FileResponse(index)


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
    foto_etiqueta: Optional[UploadFile] = File(None),
    foto_equipo:   Optional[UploadFile] = File(None),
):
    # Subir imágenes a Google Drive
    url_etiqueta = await procesar_imagen(foto_etiqueta)
    url_equipo   = await procesar_imagen(foto_equipo)
    id_unico     = str(uuid.uuid4())

    data = dict(
        id_unico=id_unico, codigo_bien=codigo_bien, codigo_auxiliar=codigo_auxiliar,
        tipo=tipo, marca=marca, modelo=modelo, serie=serie,
        sistema_operativo=sistema_operativo, ram=ram, procesador=procesador,
        dependencia=dependencia, ubicacion=ubicacion, ciudad=ciudad,
        cedula_custodio=cedula_custodio, responsable=responsable,
        ip=ip, mac=mac, hostname=hostname, estado=estado,
        foto_equipo=url_equipo or "",
        foto_etiqueta=url_etiqueta or "",
        ocr_raw=ocr_raw,
    )

    try:
        escribir_sheets(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error Google Sheets: {e}")

    return {
        "ok": True,
        "id_unico": id_unico,
        "foto_equipo": url_equipo,
        "foto_etiqueta": url_etiqueta,
        "message": "✅ Guardado en Google Sheets y fotos en Drive"
    }


@app.get("/api/inventario")
def listar():
    try:
        return leer_sheets()
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

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

# ─── Cloudflare R2 Storage (S3 Compatible) ────────────────────────────────────
R2_ENDPOINT   = os.environ.get("R2_ENDPOINT", "https://48540cc26871e66437e96bc37d8cea4a.r2.cloudflarestorage.com").strip()
R2_BUCKET     = os.environ.get("R2_BUCKET", "inventario").strip()
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").strip()

def subir_a_r2(contenido: bytes, nombre: str, mime: str = "image/jpeg") -> Optional[str]:
    """Sube una fotografía a Cloudflare R2 y retorna la URL pública permanente."""
    if not (R2_ACCESS_KEY and R2_SECRET_KEY):
        return None
    try:
        import boto3
        from botocore.config import Config
        s3 = boto3.client(
            service_name="s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto"
        )
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=nombre,
            Body=contenido,
            ContentType=mime
        )
        if R2_PUBLIC_URL:
            base = R2_PUBLIC_URL.rstrip('/')
            url = f"{base}/{nombre}"
        else:
            url = f"{R2_ENDPOINT}/{R2_BUCKET}/{nombre}"
        print(f"Foto subida a Cloudflare R2 con éxito: {url}")
        return url
    except Exception as ex_r2:
        print(f"Aviso Cloudflare R2: {ex_r2}, probando siguiente almacenamiento...")
        return None

# ─── Google Drive ──────────────────────────────────────────────────────────────
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "").strip() or "1JYYxW_iFPDVXOuySOCbsiv6oWOBB3c5w"

def subir_a_drive(contenido: bytes, nombre: str, mime: str = "image/jpeg") -> str:
    """
    Sube un archivo a Google Drive mediante la API REST v3 y retorna la URL pública.
    """
    import io, json, requests
    import google.auth.transport.requests

    folder_id = os.environ.get("DRIVE_FOLDER_ID", "").strip() or DRIVE_FOLDER_ID

    try:
        creds = get_google_creds()
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        token = creds.token

        meta = {"name": nombre}
        if folder_id:
            meta["parents"] = [folder_id]

        r_up = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true",
            headers={"Authorization": f"Bearer {token}"},
            files={
                "metadata": (None, json.dumps(meta), "application/json; charset=UTF-8"),
                "file": (nombre, contenido, mime)
            },
            timeout=3
        )
        if r_up.status_code in (200, 201):
            file_id = r_up.json().get("id")
            try:
                requests.post(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions?supportsAllDrives=true",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"type": "anyone", "role": "reader"},
                    timeout=2
                )
            except:
                pass
            return f"https://drive.google.com/thumbnail?id={file_id}&sz=w600"
    except Exception as ex_d:
        print(f"Aviso Drive: {ex_d}, usando fallback de imagen...")

    return ""

def subir_foto_nube(contenido: bytes, nombre: str, mime: str = "image/jpeg") -> str:
    """
    Cadena de almacenamiento de fotos:
    1. Cloudflare R2 (si tiene claves configuradas)
    2. Google Drive institucional
    3. Fallback de alta disponibilidad
    """
    # 1. Cloudflare R2
    url_r2 = subir_a_r2(contenido, nombre, mime)
    if url_r2:
        return url_r2

    # 2. Google Drive
    url_drive = subir_a_drive(contenido, nombre, mime)
    if url_drive:
        return url_drive

    # 3. Fallback
    try:
        import io, requests
        r_fb = requests.post(
            "https://freeimage.host/api/1/upload",
            data={"key": "6d207e02198a847aa98d0a2a901485a5", "action": "upload", "format": "json"},
            files={"source": (nombre, io.BytesIO(contenido), mime)},
            timeout=7
        )
        if r_fb.status_code == 200:
            url_img = r_fb.json().get("image", {}).get("url")
            if url_img:
                return url_img
    except Exception as ex_fb:
        print(f"Error en fallback de imagen: {ex_fb}")

    return ""

async def procesar_imagen(archivo: Optional[UploadFile]) -> Optional[str]:
    """Lee el UploadFile, lo sube a R2 / Drive y retorna la URL sin bloquear la app."""
    if not archivo or not archivo.filename:
        return None
    try:
        contenido = await archivo.read()
        ext  = os.path.splitext(archivo.filename)[1].lower() or ".jpg"
        mime = "image/png" if ext == ".png" else "image/jpeg"
        nombre = f"inventario_{uuid.uuid4().hex}{ext}"
        return subir_foto_nube(contenido, nombre, mime)
    except Exception as e:
        print(f"Aviso: No se pudo subir foto: {e}")
        return None

# ─── Helpers Sheets ────────────────────────────────────────────────────────────
def col_idx(headers: list, name: str) -> Optional[int]:
    try:    return headers.index(name)
    except: return None

def tipo_prefix(tipo: str) -> str:
    t = (tipo or "").lower()
    for k in ["cpu", "monitor", "teclado", "mouse", "impresora"]:
        if k in t:
            return k.capitalize()
    if "escritorio" in t or "torre" in t: return "CPU"
    if "portatil" in t or "portátil" in t: return "CPU"
    if "uno" in t: return "CPU"
    return "Otro"

def img(url: str) -> str:
    """Guarda la foto como hipervínculo clicable en Google Sheets (compatible con todos los hosts)."""
    if url and url.startswith("http"):
        url_escaped = url.replace('"', '%22')
        return f'=HYPERLINK("{url_escaped}";"Ver foto")'
    return ""

def limpiar_ocr(texto: str) -> str:
    """Limpia el texto OCR para guardar solo información técnica esencial, sin textos de IA ni viñetas."""
    if not texto:
        return ""
    lineas = []
    for l in texto.splitlines():
        l_s = l.strip()
        # Filtrar encabezados de IA o emojis
        if any(w in l_s.lower() for w in ["agente ia", "gemini", "identificación completa", "robot"]):
            continue
        if any(w in l_s.lower() for w in ["etiqueta de inventario", "etiqueta de activo", "la marca se determina", "info:"]):
            continue
        # Limpiar caracteres especiales / viñetas
        l_s = "".join(c for c in l_s if ord(c) < 10000).lstrip("•*- ").strip()
        if l_s and not any(w in l_s.lower() for w in ["agente ia", "gemini", "info:"]):
            lineas.append(l_s)
    return " | ".join(lineas) if lineas else texto.strip()

FOTO_COLS = {
    "CPU":       ("Foto General CPU",       "Foto Etiqueta CPU",       "OCR CPU"),
    "Monitor":   ("Foto General Monitor",   "Foto Etiqueta Monitor",   "OCR Monitor"),
    "Teclado":   ("Foto General Teclado",   "Foto Etiqueta Teclado",   "OCR Teclado"),
    "Mouse":     ("Foto General Mouse",     "Foto Etiqueta Mouse",     "OCR Mouse"),
    "Impresora": ("Foto General Impresora", "Foto Etiqueta Impresora", "OCR Impresora"),
}

def escribir_sheets(data: dict) -> tuple[int, bool]:
    import gspread.utils
    ws      = get_sheet()
    headers = ws.row_values(1)
    
    fila_num = None
    if data.get("fila"):
        try:
            fila_num = int(data.get("fila"))
        except (ValueError, TypeError):
            fila_num = None

    todas_filas = ws.get_all_values()
    tipo_str = (data.get("tipo") or "").upper()
    es_periferico = any(p in tipo_str for p in ["MONITOR", "TECLADO", "MOUSE", "IMPRESORA", "UPS", "REGULADOR", "ESCANER", "ESCÁNER", "LECTOR"])

    col_tipo_i = col_idx(headers, "Tipo de bien")

    # Si se pasó fila explícita, pero es periférico y la fila es un computador, NO sobreescribir el computador
    if fila_num and fila_num <= len(todas_filas) and es_periferico:
        r_actual = todas_filas[fila_num - 1]
        t_actual = r_actual[col_tipo_i].upper() if col_tipo_i is not None and col_tipo_i < len(r_actual) else ""
        if any(c in t_actual for c in ["COMPUTADOR", "PORTÁTIL", "PORTATIL", "ESCRITORIO", "TODO EN UNO", "SERVIDOR", "CPU"]):
            fila_num = None  # Forzar creación de fila nueva para el periférico

    # Si no se pasó fila explícita, buscar inteligentemente coincidencia
    if not fila_num and len(todas_filas) > 1:
        cod_bien  = (data.get("codigo_bien") or "").strip().upper()
        serie_eq  = (data.get("serie") or "").strip().upper()
        cod_aux   = (data.get("codigo_auxiliar") or "").strip().upper()
        resp_cust = (data.get("responsable") or "").strip().upper()
        ced_cust  = (data.get("cedula_custodio") or "").strip().upper()

        col_bien_i = col_idx(headers, "Código del bien IESS")
        col_ser_i  = col_idx(headers, "Serie del equipo")
        col_aux_i  = col_idx(headers, "Código Auxiliar (Unnamed: 1)")
        col_resp_i = col_idx(headers, "Nombre del Custodio")
        col_ced_i  = col_idx(headers, "Cédula Custodio")

        if not es_periferico:
            # 1. Prioridad para computadores: Coincidencia Custodio + (Serie o Código Bien)
            if (resp_cust or ced_cust) and (serie_eq or cod_bien):
                for num_f in range(len(todas_filas), 1, -1):
                    r = todas_filas[num_f - 1]
                    val_resp = r[col_resp_i].strip().upper() if col_resp_i is not None and col_resp_i < len(r) else ""
                    val_ced  = r[col_ced_i].strip().upper()  if col_ced_i is not None and col_ced_i < len(r) else ""
                    val_ser  = r[col_ser_i].strip().upper()  if col_ser_i is not None and col_ser_i < len(r) else ""
                    val_bien = r[col_bien_i].strip().upper() if col_bien_i is not None and col_bien_i < len(r) else ""
                    cust_match = (resp_cust and (resp_cust in val_resp or val_resp in resp_cust)) or (ced_cust and ced_cust == val_ced)
                    eq_match   = (serie_eq and val_ser == serie_eq) or (cod_bien and val_bien == cod_bien)
                    if cust_match and eq_match:
                        fila_num = num_f
                        break

            # 2. Prioridad: Coincidencia doble (Código Bien Y Serie)
            if not fila_num and cod_bien and serie_eq and len(serie_eq) > 3:
                for num_f in range(len(todas_filas), 1, -1):
                    r = todas_filas[num_f - 1]
                    val_bien = r[col_bien_i].strip().upper() if col_bien_i is not None and col_bien_i < len(r) else ""
                    val_ser  = r[col_ser_i].strip().upper()  if col_ser_i is not None and col_ser_i < len(r) else ""
                    if val_bien == cod_bien and val_ser == serie_eq:
                        fila_num = num_f
                        break

            # 3. Coincidencia por Código del Bien IESS
            if not fila_num and cod_bien:
                for num_f in range(len(todas_filas), 1, -1):
                    r = todas_filas[num_f - 1]
                    val_bien = r[col_bien_i].strip().upper() if col_bien_i is not None and col_bien_i < len(r) else ""
                    if val_bien == cod_bien:
                        fila_num = num_f
                        break

            # 4. Coincidencia por Serie del equipo
            if not fila_num and serie_eq and len(serie_eq) > 3:
                for num_f in range(len(todas_filas), 1, -1):
                    r = todas_filas[num_f - 1]
                    val_ser  = r[col_ser_i].strip().upper() if col_ser_i is not None and col_ser_i < len(r) else ""
                    if val_ser == serie_eq:
                        fila_num = num_f
                        break

            # 5. Coincidencia por Código Auxiliar
            if not fila_num and cod_aux and len(cod_aux) > 5:
                for num_f in range(len(todas_filas), 1, -1):
                    r = todas_filas[num_f - 1]
                    val_aux  = r[col_aux_i].strip().upper() if col_aux_i is not None and col_aux_i < len(r) else ""
                    if val_aux == cod_aux:
                        fila_num = num_f
                        break
        else:
            # Para periféricos: SOLO coincidir si la fila ya es del mismo tipo de periférico por Serie o Código de Bien
            for num_f in range(len(todas_filas), 1, -1):
                r = todas_filas[num_f - 1]
                t_f = r[col_tipo_i].upper() if col_tipo_i is not None and col_tipo_i < len(r) else ""
                val_bien = r[col_bien_i].strip().upper() if col_bien_i is not None and col_bien_i < len(r) else ""
                val_ser  = r[col_ser_i].strip().upper()  if col_ser_i is not None and col_ser_i < len(r) else ""
                val_aux  = r[col_aux_i].strip().upper()  if col_aux_i is not None and col_aux_i < len(r) else ""

                mismo_periferico = (cod_bien and val_bien == cod_bien) or \
                                   (serie_eq and len(serie_eq) > 3 and val_ser == serie_eq) or \
                                   (cod_aux and len(cod_aux) > 5 and val_aux == cod_aux)

                if mismo_periferico and (tipo_str in t_f or not any(c in t_f for c in ["COMPUTADOR", "PORTÁTIL", "ESCRITORIO", "TODO EN UNO"])):
                    fila_num = num_f
                    break

    from datetime import date as _date
    fecha_inv = data.get("fecha_inventario") or _date.today().strftime("%Y-%m-%d")

    # Si encontramos o recibimos una fila existente (> 1), completamos esa fila sin duplicar
    if fila_num and fila_num > 1:
        if fila_num <= len(todas_filas):
            valores_actuales = list(todas_filas[fila_num - 1])
        else:
            valores_actuales = ws.row_values(fila_num)

        fila = valores_actuales + [""] * max(0, len(headers) - len(valores_actuales))

        def sc(col, val):
            i = col_idx(headers, col)
            if i is not None and val:
                fila[i] = str(val)

        sc("Fecha de Inventario", fecha_inv)

        if not es_periferico:
            # Actualizar campos del computador principal
            if data.get("codigo_bien"):       sc("Código del bien IESS", data["codigo_bien"])
            if data.get("codigo_auxiliar"):   sc("Código Auxiliar (Unnamed: 1)", data["codigo_auxiliar"])
            if data.get("marca"):             sc("Marca del equipo", data["marca"])
            if data.get("modelo"):            sc("Modelo del equipo", data["modelo"])
            if data.get("serie"):             sc("Serie del equipo", data["serie"])
            if data.get("sistema_operativo"): sc("Sistema Operativo", data["sistema_operativo"])
            if data.get("ram"):               sc("Memoria RAM", data["ram"])
            if data.get("procesador"):        sc("Procesador", data["procesador"])
            if data.get("disco_tam"):         sc("Tamaño del Disco", data["disco_tam"])
            if data.get("disco_unidad"):      sc("Unidad Disco", data["disco_unidad"])
            if data.get("hostname"):          sc("Nombre completo del equipo (hostname)", data["hostname"])
            if data.get("ip"):                sc("Dirección IP", data["ip"])
            if data.get("mac"):               sc("MAC Address", data["mac"])
            if data.get("usuarios"):          sc("Usuarios Perfiles", data["usuarios"])
            if data.get("dependencia"):       sc("Dependencia/Edificio", data["dependencia"])
            if data.get("ubicacion"):         sc("Ubicación / Area Funcional", data["ubicacion"])
            if data.get("responsable"):       sc("Nombre del Custodio", data["responsable"])
            if data.get("cedula_custodio"):   sc("Cédula Custodio", data["cedula_custodio"])
            if data.get("proveedor"):         sc("Proveedor del equipo", data["proveedor"])
            if data.get("ciudad"):            sc("Ciudad", data["ciudad"])
            if data.get("estado"):            sc("Operativo", "SI" if data["estado"] == "Bueno" else "NO")

            if "TODO EN UNO" in tipo_str or "AIO" in tipo_str:
                sc("COMPUTADOR TODO EN UNO", "1")
            elif "PORTÁTIL" in tipo_str or "PORTATIL" in tipo_str:
                sc("COMPUTADOR PORTÁTIL", "1")
            elif "ESCRITORIO" in tipo_str or "CPU" in tipo_str:
                sc("COMPUTADOR DE ESCRITORIO", "1")

            ocr_val = limpiar_ocr(data.get("ocr_raw", ""))
            prefix = tipo_prefix(tipo_str)
            if prefix in FOTO_COLS:
                c_eq, c_et, c_ocr = FOTO_COLS[prefix]
                if data.get("foto_equipo"):   sc(c_eq, img(data["foto_equipo"]))
                if data.get("foto_etiqueta"): sc(c_et, img(data["foto_etiqueta"]))
                if ocr_val:                   sc(c_ocr, ocr_val)
            else:
                if data.get("foto_equipo"):   sc("Foto General CPU", img(data["foto_equipo"]))
                if data.get("foto_etiqueta"): sc("Foto Etiqueta CPU", img(data["foto_etiqueta"]))
                if ocr_val:                   sc("OCR CPU", ocr_val)
        else:
            # Periférico: actualizar sus propios datos en su fila independiente
            if data.get("codigo_bien"):       sc("Código del bien IESS", data["codigo_bien"])
            if data.get("codigo_auxiliar"):   sc("Código Auxiliar (Unnamed: 1)", data["codigo_auxiliar"])
            if data.get("marca"):             sc("Marca del equipo", data["marca"])
            if data.get("modelo"):            sc("Modelo del equipo", data["modelo"])
            if data.get("serie"):             sc("Serie del equipo", data["serie"])
            if data.get("dependencia"):       sc("Dependencia/Edificio", data["dependencia"])
            if data.get("ubicacion"):         sc("Ubicación / Area Funcional", data["ubicacion"])
            if data.get("responsable"):       sc("Nombre del Custodio", data["responsable"])
            if data.get("cedula_custodio"):   sc("Cédula Custodio", data["cedula_custodio"])
            if data.get("ciudad"):            sc("Ciudad", data["ciudad"])
            if data.get("estado"):            sc("Operativo", "SI" if data["estado"] == "Bueno" else "NO")
            if data.get("id_equipo_principal"): sc("ID_Equipo_Principal", data["id_equipo_principal"])

            ocr_val = limpiar_ocr(data.get("ocr_raw", ""))
            prefix = tipo_prefix(tipo_str)
            if prefix in FOTO_COLS:
                c_eq, c_et, c_ocr = FOTO_COLS[prefix]
                if data.get("foto_equipo"):   sc(c_eq, img(data["foto_equipo"]))
                if data.get("foto_etiqueta"): sc(c_et, img(data["foto_etiqueta"]))
                if ocr_val:                   sc(c_ocr, ocr_val)
            else:
                if data.get("foto_equipo"):   sc("Foto General CPU", img(data["foto_equipo"]))
                if data.get("foto_etiqueta"): sc("Foto Etiqueta CPU", img(data["foto_etiqueta"]))
                if ocr_val:                   sc("OCR CPU", ocr_val)

        col_end = gspread.utils.rowcol_to_a1(1, len(fila)).rstrip("0123456789")
        ws.update([fila], f"A{fila_num}:{col_end}{fila_num}", value_input_option="USER_ENTERED")
        return fila_num, True

    # Si NO existe, es registro nuevo: append_row (CADA BIEN TIENE SU PROPIA FILA)
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
    sc("Dependencia/Edificio",                   data.get("dependencia", "C.C.Q.A.H.D. COTOCOLLAO"))
    sc("Ubicación / Area Funcional",             data.get("ubicacion", ""))
    sc("Cédula Custodio",                        data.get("cedula_custodio", ""))
    sc("Nombre del Custodio",                    data.get("responsable", ""))
    sc("Tiene Garantía",                         data.get("garantia", "NO"))
    sc("Proveedor del equipo",                   data.get("proveedor", ""))
    sc("Operativo",                              "SI" if data.get("estado","Bueno") == "Bueno" else "NO")
    sc("Dirección IP",                           data.get("ip", ""))
    sc("MAC Address",                            data.get("mac", ""))
    sc("Nombre completo del equipo (hostname)",  data.get("hostname", ""))
    
    id_unico = data.get("id_unico", str(uuid.uuid4()))
    sc("ID_Unico",            f"{fecha_inv} | {id_unico[:8].upper()}")
    sc("Fecha de Inventario", fecha_inv)
    sc("ID_Equipo_Principal", data.get("id_equipo_principal", ""))

    # Marcar casilla del tipo
    if "TODO EN UNO" in tipo_str or "AIO" in tipo_str:
        sc("COMPUTADOR TODO EN UNO", "1")
    elif "PORTÁTIL" in tipo_str or "PORTATIL" in tipo_str:
        sc("COMPUTADOR PORTÁTIL", "1")
    elif "ESCRITORIO" in tipo_str or "CPU" in tipo_str:
        sc("COMPUTADOR DE ESCRITORIO", "1")
    elif "SERVIDOR" in tipo_str:
        sc("SERVIDOR FÍSICO", "1")

    ocr_val = limpiar_ocr(data.get("ocr_raw", ""))
    prefix = tipo_prefix(data.get("tipo", ""))
    if prefix in FOTO_COLS:
        c_eq, c_et, c_ocr = FOTO_COLS[prefix]
        sc(c_eq,  img(data.get("foto_equipo", "")))
        sc(c_et,  img(data.get("foto_etiqueta", "")))
        if ocr_val: sc(c_ocr, ocr_val)
    else:
        sc("Foto General CPU",  img(data.get("foto_equipo", "")))
        sc("Foto Etiqueta CPU", img(data.get("foto_etiqueta", "")))
        if ocr_val: sc("OCR CPU", ocr_val)

    ws.append_row(fila, value_input_option="USER_ENTERED")
    nueva_fila = len(todas_filas) + 1
    return nueva_fila, False


def leer_sheets() -> list:
    ws       = get_sheet()
    all_vals = ws.get_all_values()
    if not all_vals or len(all_vals) < 2:
        return []
    headers = all_vals[0]

    def get_col(row, col_name):
        idx = col_idx(headers, col_name)
        if idx is not None and idx < len(row):
            v = row[idx]
            return str(v).strip() if v is not None else ""
        return ""

    result = []
    for i in range(1, len(all_vals)):
        r = all_vals[i]
        num_fila = i + 1

        tipo = get_col(r, "Tipo de bien")
        if not tipo:
            if get_col(r, "COMPUTADOR TODO EN UNO") in ("1", "SI", "X"):
                tipo = "COMPUTADOR TODO EN UNO"
            elif get_col(r, "COMPUTADOR PORTÁTIL") in ("1", "SI", "X"):
                tipo = "COMPUTADOR PORTÁTIL"
            elif get_col(r, "COMPUTADOR DE ESCRITORIO") in ("1", "SI", "X"):
                tipo = "COMPUTADOR DE ESCRITORIO"
            elif get_col(r, "SERVIDOR FÍSICO") in ("1", "SI", "X"):
                tipo = "SERVIDOR"
            elif get_col(r, "Serie del equipo") or get_col(r, "Código del bien IESS") or get_col(r, "Nombre del Custodio"):
                tipo = "COMPUTADOR DE ESCRITORIO"
            else:
                continue

        result.append({
            "fila":          num_fila,
            "id_unico":      get_col(r, "ID_Unico"),
            "codigo_bien":   get_col(r, "Código del bien IESS"),
            "codigo_aux":    get_col(r, "Código Auxiliar (Unnamed: 1)"),
            "tipo":          tipo,
            "marca":         get_col(r, "Marca del equipo"),
            "modelo":        get_col(r, "Modelo del equipo"),
            "serie":         get_col(r, "Serie del equipo"),
            "sistema_op":    get_col(r, "Sistema Operativo"),
            "ram":           get_col(r, "Memoria RAM"),
            "procesador":    get_col(r, "Procesador"),
            "dependencia":   get_col(r, "Dependencia/Edificio"),
            "ubicacion":     get_col(r, "Ubicación / Area Funcional"),
            "responsable":   get_col(r, "Nombre del Custodio"),
            "cedula":        get_col(r, "Cédula Custodio"),
            "operativo":     get_col(r, "Operativo"),
            "ip":            get_col(r, "Dirección IP"),
            "mac":           get_col(r, "MAC Address"),
            "hostname":      get_col(r, "Nombre completo del equipo (hostname)"),
            "foto_cpu":      get_col(r, "Foto General CPU"),
            "foto_monitor":  get_col(r, "Foto General Monitor"),
            "foto_teclado":  get_col(r, "Foto General Teclado"),
            "foto_mouse":    get_col(r, "Foto General Mouse"),
            "foto_impresora":get_col(r, "Foto General Impresora"),
            "id_equipo_principal": get_col(r, "ID_Equipo_Principal"),
            "fecha_inventario":    get_col(r, "Fecha de Inventario"),
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


@app.get("/iess-blue.jpeg", include_in_schema=False)
@app.get("/images/iess-blue.jpeg", include_in_schema=False)
def get_banner_blue():
    for p in [
        os.path.join(os.path.dirname(__file__), "images", "iess-blue.jpeg"),
        os.path.join(static_dir, "iess-blue.jpeg")
    ]:
        if os.path.exists(p):
            return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="Imagen no encontrada")


@app.get("/logo_iess.jpg", include_in_schema=False)
def get_logo():
    logo_path = os.path.join(static_dir, "logo_iess.jpg")
    if os.path.exists(logo_path):
        return FileResponse(logo_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="Logo no encontrado")


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
    disco_tam:         str  = Form(""),
    disco_unidad:      str  = Form("GB"),
    ip:                str  = Form(""),
    mac:               str  = Form(""),
    hostname:          str  = Form(""),
    usuarios:          str  = Form(""),
    codigo_bien:       str  = Form(""),
    codigo_auxiliar:   str  = Form(""),
    proveedor:         str  = Form(""),
    ciudad:            str  = Form("QUITO"),
    estado:            str  = Form("Bueno"),
    ocr_raw:           str  = Form(""),
    fila:              Optional[int] = Form(None),
    id_equipo_principal: str = Form(""),
    fecha_inventario:    str = Form(""),
    foto_etiqueta: Optional[UploadFile] = File(None),
    foto_auxiliar: Optional[UploadFile] = File(None),
    foto_equipo:   Optional[UploadFile] = File(None),
):
    import asyncio

    # Subir imágenes a Google Drive en paralelo (con timeout seguro para no bloquear Sheets)
    async def _safe_subir(archivo):
        if not archivo or not archivo.filename:
            return None
        try:
            return await asyncio.wait_for(procesar_imagen(archivo), timeout=7.0)
        except Exception as ex_f:
            print(f"Aviso al procesar foto {getattr(archivo, 'filename', '')}: {ex_f}")
            return None

    url_etiqueta, url_auxiliar, url_equipo = await asyncio.gather(
        _safe_subir(foto_etiqueta),
        _safe_subir(foto_auxiliar),
        _safe_subir(foto_equipo),
    )
    id_unico = str(uuid.uuid4())

    # Combinar URLs de fotos de etiquetas si ambas existen
    urls_et = [u for u in [url_etiqueta, url_auxiliar] if u]
    foto_etiqueta_final = "\n".join(urls_et) if urls_et else ""

    data = dict(
        id_unico=id_unico, codigo_bien=codigo_bien, codigo_auxiliar=codigo_auxiliar,
        tipo=tipo, marca=marca, modelo=modelo, serie=serie,
        sistema_operativo=sistema_operativo, ram=ram, procesador=procesador,
        disco_tam=disco_tam, disco_unidad=disco_unidad,
        dependencia=dependencia, ubicacion=ubicacion, ciudad=ciudad,
        cedula_custodio=cedula_custodio, responsable=responsable,
        proveedor=proveedor, usuarios=usuarios,
        ip=ip, mac=mac, hostname=hostname, estado=estado,
        id_equipo_principal=id_equipo_principal,
        fecha_inventario=fecha_inventario,
        foto_equipo=url_equipo or "",
        foto_etiqueta=foto_etiqueta_final,
        ocr_raw=ocr_raw,
        fila=fila,
    )

    try:
        fila_guardada, fue_actualizado = escribir_sheets(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error Google Sheets: {e}")

    if fue_actualizado:
        msg = f"✅ Información y fotos de {tipo} completadas con éxito en la fila {fila_guardada}"
    else:
        msg = f"✅ {tipo} registrado como nuevo equipo en fila {fila_guardada}"

    return {
        "ok": True,
        "id_unico": id_unico,
        "fila": fila_guardada,
        "actualizado": fue_actualizado,
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
    folder_id_env = os.environ.get("DRIVE_FOLDER_ID", "").strip() or DRIVE_FOLDER_ID
    resultado["drive_folder_id_configurado"] = bool(folder_id_env)
    resultado["drive_folder_id"] = folder_id_env or "No configurado"
    try:
        url_d = subir_a_drive(b"test", "test.txt", "text/plain")
        resultado["drive_ok"] = True
        resultado["drive_url"] = url_d
    except Exception as ex_d:
        resultado["drive_ok"] = False
        resultado["drive_error"] = str(ex_d)
        if "storageQuotaExceeded" in str(ex_d) or not folder_id_env:
            resultado["drive_solucion"] = "Crea una carpeta en Google Drive, compártela como Editor con 'inventario-app@inventario-506717.iam.gserviceaccount.com' y pon su ID en la variable DRIVE_FOLDER_ID en Vercel."

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
- "observaciones": NO agregues textos de relleno, párrafos explicativos ni introducciones. Deja "" a menos que haya un daño físico severo.
- "texto_leido": Solo la transcripción limpia y directa de los datos visibles en la etiqueta.
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

@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo Recopilando informacion del sistema...

:: ── Hostname ──────────────────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic computersystem get Name') do (
    if not defined HOSTNAME set "HOSTNAME=%%i"
)

:: ── Sistema Operativo ─────────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic os get Caption') do (
    if not defined SO set "SO=%%i"
)

:: ── RAM total en GB ───────────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic computersystem get TotalPhysicalMemory') do (
    if not defined RAM_BYTES set "RAM_BYTES=%%i"
)
set /a RAM_GB=RAM_BYTES/1073741824
if %RAM_GB% EQU 0 set RAM_GB=1

:: ── Procesador ────────────────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic cpu get Name') do (
    if not defined CPU set "CPU=%%i"
)

:: ── Disco principal en GB ─────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic diskdrive where "Index=0" get Size') do (
    if not defined DISK_BYTES set "DISK_BYTES=%%i"
)
set /a DISK_GB=DISK_BYTES/1073741824

:: ── Marca del equipo ──────────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic computersystem get Manufacturer') do (
    if not defined MARCA set "MARCA=%%i"
)

:: ── Modelo del equipo ─────────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic computersystem get Model') do (
    if not defined MODELO set "MODELO=%%i"
)

:: ── Número de Serie ───────────────────────────────
for /f "skip=1 tokens=*" %%i in ('wmic bios get SerialNumber') do (
    if not defined SERIE set "SERIE=%%i"
)

:: ── Dirección MAC ─────────────────────────────────
for /f "tokens=*" %%i in ('getmac /fo list /v ^| findstr "Dirección física"') do (
    if not defined MAC (
        set "LINEA=%%i"
        set "MAC=!LINEA:Dirección física. . . . . . . . . : =!"
        set "MAC=!LINEA:Physical Address. . . . . . . . . : =!"
    )
)
if not defined MAC (
    for /f "tokens=12" %%i in ('ipconfig /all ^| findstr "Dirección física"') do (
        if not defined MAC set "MAC=%%i"
    )
)

:: ── Dirección IP ──────────────────────────────────
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /c:"IPv4"') do (
    if not defined IP (
        set "IP=%%i"
        set "IP=!IP: =!"
    )
)

:: ── Tipo de equipo (Desktop / Laptop / AIO) ───────
set "IS_AIO=0"
for /f "skip=1 tokens=*" %%i in ('wmic csenclosure get ChassisTypes 2^>nul') do (
    for %%c in (%%i) do (
        if "%%c"=="13" set "IS_AIO=1"
    )
)
echo !MODELO! | findstr /i "AIO All-in-One All_in_One ProOne OptiPlex.52 OptiPlex.74 TouchSmart IdeaCentre" >nul
if !errorlevel! equ 0 set "IS_AIO=1"

for /f "skip=1 tokens=*" %%i in ('wmic computersystem get PCSystemType 2^>nul') do (
    if not defined PCTYPE set "PCTYPE=%%i"
)

if "!IS_AIO!"=="1" (
    set "TIPO=COMPUTADOR TODO EN UNO"
) else (
    if "%PCTYPE%"=="1" set "TIPO=COMPUTADOR DE ESCRITORIO"
    if "%PCTYPE%"=="2" set "TIPO=COMPUTADOR PORTÁTIL"
    if "%PCTYPE%"=="3" set "TIPO=COMPUTADOR PORTÁTIL"
    if not defined TIPO set "TIPO=COMPUTADOR DE ESCRITORIO"
)

:: ── Usuarios con perfil local ─────────────────────
set "USUARIOS="
for /f "skip=1 tokens=*" %%i in ('wmic useraccount where "LocalAccount=True and Disabled=False" get Name') do (
    if not defined USUARIOS (set "USUARIOS=%%i") else (set "USUARIOS=!USUARIOS!, %%i")
)

:: ── Datos opcionales de Etiqueta / Placa IESS ──────
echo.
echo ========================================================
echo  IDENTIFICACION INSTITUCIONAL (PLACA / ETIQUETA IESS)
echo ========================================================
echo  Si el equipo tiene etiqueta o placa del IESS visible,
echo  puedes digitarla ahora (o presiona ENTER para omitir):
echo.
set "COD_BIEN="
set /p "COD_BIEN= Codigo del Bien IESS (ej: IM-0511): "
set "COD_AUX="
set /p "COD_AUX= Codigo Auxiliar / Barras (ej: 27038980000661): "

:: ── Fecha de inventario (hoy) ────────────────────
for /f "tokens=1-3 delims=/ " %%a in ('date /t') do (
    set "DIA=%%a"
    set "MES=%%b"
    set "ANIO=%%c"
)
set "FECHA_INV=%ANIO%-%MES%-%DIA%"

:: ── Generar JSON ──────────────────────────────────
set "OUTFILE=%~dp0info.json"

(
echo {
echo   "hostname": "%HOSTNAME%",
echo   "sistema_operativo": "%SO%",
echo   "ram": "%RAM_GB% GB",
echo   "procesador": "%CPU%",
echo   "disco_tam": "%DISK_GB%",
echo   "disco_unidad": "GB",
echo   "marca": "%MARCA%",
echo   "modelo": "%MODELO%",
echo   "serie": "%SERIE%",
echo   "mac": "%MAC%",
echo   "ip": "%IP%",
echo   "tipo": "%TIPO%",
echo   "codigo_bien": "!COD_BIEN!",
echo   "codigo_auxiliar": "!COD_AUX!",
echo   "usuarios": "%USUARIOS%",
echo   "fecha_inventario": "%FECHA_INV%"
echo }
) > "%OUTFILE%"

echo.
echo ============================================
echo  Informacion recopilada correctamente!
echo  Archivo generado: info.json
echo ============================================
echo.
echo  Datos del equipo:
echo  - Hostname    : %HOSTNAME%
echo  - Sistema Op. : %SO%
echo  - RAM         : %RAM_GB% GB
echo  - Procesador  : %CPU%
echo  - Disco       : %DISK_GB% GB
echo  - Marca       : %MARCA%
echo  - Modelo      : %MODELO%
echo  - Serie       : %SERIE%
echo  - MAC         : %MAC%
echo  - IP          : %IP%
echo  - Tipo        : %TIPO%
echo.
echo  Sube el archivo info.json al sistema de inventario.
echo.
pause

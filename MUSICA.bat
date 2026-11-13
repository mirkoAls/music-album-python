@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem Sin argumentos recorre TODA la biblioteca: una carpeta de primer nivel
rem por turno, en orden alfabetico y con sus subcarpetas dentro. El bucle va
rem DENTRO del programa, en un solo proceso, para no rehacer las lecturas ni el
rem contexto global en cada carpeta.
rem Con un argumento hace solo esa carpeta, subcarpeta incluida:
rem     MUSICA.bat "cumbia\la champions liga"
rem No llama a MUSICA.ps1: va directamente al programa Python.
set "BIBLIOTECA=D:\Musica\ZLast"
set "PY=%~dp0.venv\Scripts\python.exe"
set "MAIN=%~dp0main.py"
set "PYTHONUTF8=1"
set "FALLOS="
set "HECHAS=0"

pushd "%~dp0"

if not exist "%PY%" (
    echo.
    echo ERROR: no se encontro el Python del proyecto:
    echo   %PY%
    echo.
    echo Ejecuta primero:  python -m venv .venv
    pause
    popd
    exit /b 1
)

if not exist "%BIBLIOTECA%" (
    echo.
    echo ERROR: no existe la biblioteca:
    echo   %BIBLIOTECA%
    echo.
    echo Cambia la linea "set BIBLIOTECA=" al principio de este archivo.
    pause
    popd
    exit /b 1
)

rem ---------------------------------------------------------------- una sola
if not "%~1"=="" (
    title Arreglar musica - %~1
    call :procesar "%~1"
    goto :fin
)

rem ------------------------------------------------------- toda la biblioteca
title Arreglar musica - biblioteca completa
echo ======================================================================
echo   Se arreglara TODO en: %BIBLIOTECA%
echo   Una carpeta por turno, en orden, con sus subcarpetas dentro.
echo   Todo en un solo proceso, reutilizando las caches.
echo   Nombres, artistas, ID3, etiquetas y portadas.
echo.
echo   Las portadas que ya puso el programa no se vuelven a descargar;
echo   el resto se comprueban por si no son la del album real.
echo.
echo   Son miles de archivos y puede tardar horas. Se puede cortar con
echo   Ctrl+C: lo hecho se conserva, y al repetir no se vuelve a preguntar
echo   por las canciones que ya quedaron completas y verificadas.
echo ======================================================================
echo.
choice /c SN /n /m "Empezamos? (S = si, N = no): "
if errorlevel 2 goto :cancelado

rem Hasta tres intentos. Si el proceso muere de golpe (no por una excepcion,
rem que esas ya se saltan carpeta a carpeta, sino porque se lo lleva el
rem sistema), repetir sale barato: lo ya resuelto esta apuntado y no vuelve a
rem preguntar a ninguna fuente.
set "HECHAS=0"
set "INTENTO=0"

:reintentar
set /a INTENTO+=1
set /a HECHAS+=1
if %INTENTO% GTR 1 (
    echo.
    echo   ---------------------------------------------------------------
    echo   Reintento %INTENTO% de 3. Lo ya hecho se conserva; sigue por donde iba.
    echo   ---------------------------------------------------------------
    echo.
)
"%PY%" "%MAIN%" todo --root "%BIBLIOTECA%" --apply
set "RC=%ERRORLEVEL%"

rem 0 = todo bien. 1 = alguna carpeta dio error o se corto con Ctrl+C: en los
rem dos casos el programa termino por su cuenta y no hay nada que reintentar.
if "%RC%"=="0" goto :fin
if "%RC%"=="1" (
    set "FALLOS= [alguna carpeta con error]"
    goto :fin
)
if %INTENTO% LSS 3 goto :reintentar
set "FALLOS= [el programa murio %INTENTO% veces, ultimo error %RC%]"

goto :fin

rem -------------------------------------------------------------- subrutinas
:procesar
echo.
echo ######################################################################
echo #  CARPETA: %~1
echo ######################################################################
title Arreglar musica - %~1
"%PY%" "%MAIN%" todo --root "%BIBLIOTECA%" --carpeta "%~1" --apply
set "RC=%ERRORLEVEL%"
set /a HECHAS+=1
if not "%RC%"=="0" (
    echo.
    echo   *** "%~1" termino con error %RC%. Se sigue con la siguiente.
    set "FALLOS=%FALLOS% [%~1]"
)
goto :eof

:cancelado
echo.
echo   Cancelado. No se ha tocado nada.
echo.
pause
popd
exit /b 0

:fin
echo.
echo ======================================================================
echo   TERMINADO. Intentos: %HECHAS%
if defined FALLOS echo   Errores:%FALLOS%
echo.
echo   Todo es reversible:
echo     tags     -^> python main.py restaurar backups\^<el mas reciente^>.json
echo     nombres  -^> python main.py deshacer  logs\renombrados\renombrados-*.json
echo     portadas -^> python main.py deshacer  logs\portadas\portadas-*.json
echo ======================================================================
echo.
echo   Ahora dile a tu reproductor que vuelva a leer la biblioteca.
echo.
pause
popd
if defined FALLOS exit /b 1
exit /b 0

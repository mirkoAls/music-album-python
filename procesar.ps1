# Procesa una carpeta de la biblioteca, paso a paso y con el progreso a la vista.
#
#   .\procesar.ps1 Aleman           # informe: no toca nada
#   .\procesar.ps1 Aleman -Aplicar  # escribe tags y descarga portadas
#
# Se puede cortar con Ctrl+C en cualquier momento: lo hecho se conserva, y al
# repetir no se vuelve a consultar lo que ya esta en la cache.

param(
    [Parameter(Mandatory = $true)][string]$Carpeta,
    [switch]$Aplicar,
    [switch]$SinConsenso,
    [switch]$Verificar
)

$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$main = Join-Path $PSScriptRoot "main.py"

if (-not (Test-Path $py)) {
    Write-Host "No encuentro $py" -ForegroundColor Red
    exit 1
}

# OJO: en PowerShell las variables NO distinguen mayusculas, asi que $verificar
# y $Verificar son la misma. Por eso los argumentos llevan prefijo argX: sin el,
# asignar una cadena machaca el parametro switch y PowerShell falla al arrancar.
$argModo = if ($Aplicar) { "--apply" } else { $null }
# Por defecto se pregunta a las tres fuentes y solo se escribe el album cuando
# al menos dos coinciden. -SinConsenso vuelve al modo rapido: la primera que
# conteste manda.
$argConsenso = if ($SinConsenso) { $null } else { "--consenso" }
# -Verificar contrasta tambien los albumes que ya estan puestos, no solo los
# huecos. En carpetas de idioma (Aleman, Ruso, Frances, Ramdom) es ademas la
# unica via para que reciban genero, porque la carpeta no se lo da.
$argVerificar = if ($Verificar) { "--verificar" } else { $null }

function Paso($titulo, $argumentos) {
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor Cyan
    Write-Host "  $titulo" -ForegroundColor Cyan
    Write-Host ("=" * 70) -ForegroundColor Cyan
    & $py $main @argumentos
}

# "electro/hardwell" no vale como nombre de archivo: la barra crearia una
# subcarpeta. Se aplana para el informe.
$nombreCarpeta = $Carpeta -replace '[\\/:*?"<>|]', '-'
$nombreInforme = "informe-" + $nombreCarpeta + ".csv"

$inicio = Get-Date

Paso "1/2  Album y fecha (Deezer + iTunes + MusicBrainz)  ($Carpeta)" @(
    "scan", "--online", "--carpeta", $Carpeta, $argModo, $argConsenso, $argVerificar,
    "--csv", (Join-Path $PSScriptRoot "informes\$nombreCarpeta\$nombreInforme")
).Where({ $_ })

Paso "2/2  Portadas  ($Carpeta)" @(
    "portadas", "--carpeta", $Carpeta, $argModo, $argConsenso
).Where({ $_ })

$minutos = [math]::Round(((Get-Date) - $inicio).TotalMinutes, 1)
Write-Host ""
Write-Host ("=" * 70) -ForegroundColor Green
Write-Host "  Terminado '$Carpeta' en $minutos minutos" -ForegroundColor Green
if (-not $Aplicar) {
    Write-Host "  Era solo informe. Repite con -Aplicar para escribir." -ForegroundColor Yellow
} else {
    Write-Host "  Deshacer tags:     python main.py restaurar backups\<el-mas-reciente>.json"
    Write-Host "  Deshacer portadas: python main.py deshacer  logs\portadas\portadas-<fecha>.json"
}
Write-Host ("=" * 70) -ForegroundColor Green

# ============================================================================
#   MUSICA.ps1
#
#       .\MUSICA.ps1            mira y hace informes, no toca nada
#       .\MUSICA.ps1 -Aplicar   lo arregla todo
#
#   Para cambiar de carpeta, edita la linea $CARPETA de mas abajo.
# ============================================================================

# OJO: param() tiene que ser la PRIMERA instruccion del archivo; solo pueden ir
# comentarios por encima. Con cualquier asignacion antes, PowerShell deja de
# enlazar los parametros y -Aplicar se ignora en silencio.
param(
    [switch]$Aplicar,
    [switch]$RespetarPortadas
)

# ============================================================================
#
#            >>>  CAMBIA SOLO ESTA LINEA Y GUARDA  <<<

$CARPETA = "Aleman"

#   Ejemplos:
#       "Aleman"             una carpeta
#       "electro/hardwell"   una subcarpeta de un artista
#       ""                   TODA la biblioteca (tarda horas)
#
# ============================================================================
#
#   Lo hace TODO de una vez:
#     1. Nombres de archivo    emojis, "Official Video", "320kbps", basura
#     2. Artistas mal escritos bonnietylerVEVO -> Bonnie Tyler
#     3. ID3v2.3               para que tu reproductor lea las etiquetas
#     4. Etiquetas             titulo, artista, album, fecha y genero
#     5. Portadas              caratula real del album, no la del video
#
#   Consulta Deezer, iTunes, MusicBrainz y Last.fm, y solo escribe el album
#   cuando al menos dos coinciden. Todo es reversible.
#
#   Si prefieres conservar las portadas que ya tienes:
#       .\MUSICA.ps1 -Aplicar -RespetarPortadas
#
# ============================================================================

$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$main = Join-Path $PSScriptRoot "main.py"

if (-not (Test-Path $py)) {
    Write-Host "No encuentro $py" -ForegroundColor Red
    Write-Host "Crea el entorno con:  python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

# Las variables de PowerShell no distinguen mayusculas, asi que estas llevan
# prefijo arg para no chocar con los parametros de arriba.
$argCarpeta = @()
if ($CARPETA) { $argCarpeta = @("--carpeta", $CARPETA) }

$argAplicar = @()
if ($Aplicar) { $argAplicar = @("--apply") }

$argPortadas = @()
if ($RespetarPortadas) { $argPortadas = @("--respetar-portadas") }

$destino = if ($CARPETA) { $CARPETA } else { "TODA la biblioteca" }

Write-Host ""
Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host "   Carpeta: $destino" -ForegroundColor Cyan
if ($Aplicar) {
    Write-Host "   MODO ESCRITURA: se guardan copias de seguridad antes" -ForegroundColor Yellow
} else {
    Write-Host "   Modo informe: no se modifica ningun archivo" -ForegroundColor Green
}
Write-Host ("=" * 70) -ForegroundColor Cyan

$inicio = Get-Date

& $py $main "todo" @argCarpeta @argAplicar @argPortadas

$minutos = [math]::Round(((Get-Date) - $inicio).TotalMinutes, 1)
Write-Host ""
Write-Host ("=" * 70) -ForegroundColor Green
Write-Host "   Tardo $minutos minutos" -ForegroundColor Green
if (-not $Aplicar) {
    Write-Host "   Era solo informe. Repite con:  .\MUSICA.ps1 -Aplicar" -ForegroundColor Yellow
}
Write-Host ("=" * 70) -ForegroundColor Green

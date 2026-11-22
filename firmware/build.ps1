# Builds the ClaudeVoice firmware: upstream xiaozhi-esp32 at a pinned commit + claudevoice.patch,
# compiled with ESP-IDF 5.5.2 in Docker. Result: firmware\xiaozhi-esp32\build\xiaozhi.bin
# (copy it to ..\ota\ and devices update over Wi-Fi; first install: see README.md).
param(
    [string]$Board = "zhengchen-1.54tft-wifi"
)
$ErrorActionPreference = "Stop"
$Upstream = "https://github.com/78/xiaozhi-esp32.git"
$Commit = "e77dedb1309153bb63fed285772962c920c97dd4"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src = Join-Path $Here "xiaozhi-esp32"

if (-not (Test-Path $Src)) {
    git clone $Upstream $Src
    git -C $Src checkout $Commit
    git -C $Src apply (Join-Path $Here "claudevoice.patch")
    Write-Host "Patched xiaozhi-esp32 at $Commit"
}

docker run --rm -v "${Src}:/project" -w /project espressif/idf:v5.5.2 bash -lc `
    "git config --global --add safe.directory '*' && idf.py -DBOARD_NAME=$Board -DBOARD_TYPE=$Board build"

$Bin = Join-Path $Src "build\xiaozhi.bin"
if (Test-Path $Bin) {
    Copy-Item $Bin (Join-Path $Here "..\ota\xiaozhi.bin") -Force
    Write-Host "Done: $Bin (also copied to ota\ for Wi-Fi updates)"
}

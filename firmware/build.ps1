# Builds the ClaudeVoice universal firmware images (maintainers only; users get them from the wizard).
# Upstream xiaozhi-esp32 at a pinned commit + claudevoice.patch, ESP-IDF 5.5.2 in Docker.
# Result: firmware\images\<variant>.bin (merged images, flashed at 0x0 by the setup wizard).
param(
    [string]$Variant = "all"
)
$ErrorActionPreference = "Stop"
$Upstream = "https://github.com/78/xiaozhi-esp32.git"
$Commit = "e77dedb1309153bb63fed285772962c920c97dd4"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src = Join-Path $Here "xiaozhi-esp32"
$Images = Join-Path $Here "images"
$Variants = @{
    "claudevoice-s3-16m" = "claudevoice-s3"; "claudevoice-s3-8m" = "claudevoice-s3"
    "claudevoice-s3-8m-quad" = "claudevoice-s3"; "claudevoice-c3-4m" = "claudevoice-c3"
}

if (-not (Test-Path $Src)) {
    git clone $Upstream $Src
    git -C $Src checkout $Commit
    git -C $Src apply (Join-Path $Here "claudevoice.patch")
    Write-Host "Patched xiaozhi-esp32 at $Commit"
}
New-Item -ItemType Directory -Force $Images | Out-Null

$todo = if ($Variant -eq "all") { $Variants.Keys } else { @($Variant) }
foreach ($v in $todo) {
    $board = $Variants[$v]
    Write-Host "== $v ($board)"
    Get-ChildItem (Join-Path $Src "releases") -Filter "v*_$v.zip" -ErrorAction SilentlyContinue | Remove-Item
    docker run --rm -v "${Src}:/project" -v claudevoice-ccache:/ccache -e CCACHE_DIR=/ccache -w /project espressif/idf:v5.5.2 bash -lc `
        "git config --global --add safe.directory '*' && python scripts/release.py $board --name $v"
    $zip = Get-ChildItem (Join-Path $Src "releases") -Filter "v*_$v.zip" | Select-Object -First 1
    if (-not $zip) { throw "build of $v failed" }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [System.IO.Compression.ZipFile]::OpenRead($zip.FullName)
    $entry = $z.Entries | Where-Object Name -eq "merged-binary.bin"
    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $Images "$v.bin"), $true)
    $z.Dispose()
    Write-Host "   -> images\$v.bin"
}

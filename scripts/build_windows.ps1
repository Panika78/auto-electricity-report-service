# Build from a clean Python 3.11 x64 virtual environment on Windows.
param(
    [string]$Python = "python",
    [switch]$SkipTests
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
# Avoid Windows runner's legacy cp1252 console when printing Cyrillic QA paths.
$env:PYTHONUTF8 = "1"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Для поставки требуется Windows x64."
    }
    & $Python -c "import sys,struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) { throw "Требуется CPython 3.11 x64 на Windows." }

    # The worktree must represent precisely the recorded Git commit.
    $dirty = @(& git status --porcelain --untracked-files=normal)
    if ($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) {
        throw "Release requires clean Git checkout (tracked and untracked): $($dirty -join '; ')"
    }
    $buildCheckoutCommit = (& git rev-parse HEAD).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $buildCheckoutCommit -notmatch '^[a-f0-9]{40}$') {
        throw "Cannot resolve actual Git HEAD."
    }
    $buildTreeSha = (& git rev-parse 'HEAD^{tree}').Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $buildTreeSha -notmatch '^[a-f0-9]{40}$') {
        throw "Cannot resolve actual Git source tree."
    }
    $prHeadCommit = $env:KONTAKT_PR_HEAD_COMMIT
    if ($prHeadCommit -and $prHeadCommit -notmatch '^[a-fA-F0-9]{40}$') {
        throw "Invalid PR head commit SHA."
    }
    if ($prHeadCommit) { $prHeadCommit = $prHeadCommit.ToLowerInvariant() }

    $venv = Join-Path $root ".venv-release"
    if (Test-Path -LiteralPath $venv) {
        throw "Каталог .venv-release уже существует. Используйте чистый checkout или удалите только собственную тестовую среду вручную."
    }
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать изолированную среду." }
    $pythonExe = Join-Path $venv "Scripts\python.exe"
    & $pythonExe -m pip install --only-binary=:all: -r requirements-windows-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зафиксированные зависимости сборки." }
    & $pythonExe -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить приложение." }

    if (-not $SkipTests) {
        $env:QT_QPA_PLATFORM = "offscreen"
        & $pythonExe -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "Регрессионные тесты не прошли." }
        & $pythonExe -m compileall -q app tests scripts
        if ($LASTEXITCODE -ne 0) { throw "Ошибка компиляции Python." }
    }

    & $pythonExe -m PyInstaller --clean --noconfirm kontakt.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой." }

    $bundle = Join-Path $root "dist\kontakt"
    $exe = Join-Path $bundle "kontakt.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "В onedir отсутствует kontakt.exe."
    }
    foreach ($font in @("PT_Sans-Web-Regular.ttf", "PT_Sans-Web-Bold.ttf",
                       "PT_Serif-Web-Regular.ttf", "PT_Serif-Web-Bold.ttf",
                       "OFL-PTSans.txt", "OFL-PTSerif.txt")) {
        if (-not (Get-ChildItem -LiteralPath $bundle -Filter $font -File -Recurse)) {
            throw "Не найден необходимый ресурс: $font"
        }
    }
    foreach ($plugin in @("qwindows.dll", "qoffscreen.dll")) {
        if (-not (Get-ChildItem -LiteralPath $bundle -Filter $plugin -File -Recurse)) {
            throw "Не найден Qt platform plugin: $plugin"
        }
    }
    $leaks = @(Get-ChildItem -LiteralPath $bundle -File -Recurse | Where-Object {
        # PyInstaller requires this exact stdlib archive. All other ZIP files
        # including synthetic or user exports are still rejected.
        $knownRuntimeZip = ($_.Name -ceq "base_library.zip" -and
                            $_.DirectoryName -eq (Join-Path $bundle "_internal"))
        ($_.Name -match '(?i)\.(db|sqlite|sqlite3|pdf|bak|backup|tmp|log)$') -or
        (($_.Extension -ieq ".zip") -and -not $knownRuntimeZip) -or
        ($_.Name -match '(?i)^(signature|stamp)[-_.]')
    })
    if ($leaks.Count -ne 0) {
        throw "В дистрибутиве обнаружены подозрительные пользовательские файлы: $($leaks.FullName -join ', ')"
    }

    & $pythonExe scripts/validate_windows_bundle.py --dist $bundle
    if ($LASTEXITCODE -ne 0) { throw "В onedir есть недопустимые файлы или отсутствуют ресурсы." }
    & $pythonExe scripts/check_windows_bundle.py --dist $bundle
    if ($LASTEXITCODE -ne 0) { throw "Проверка собранного .exe завершилась с ошибкой." }

    $zip = Join-Path $root "dist\kontakt-windows-x64.zip"
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    Compress-Archive -LiteralPath $bundle -DestinationPath $zip -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $zip -PathType Leaf)) {
        throw "ZIP дистрибутива не создан."
    }

    & $pythonExe scripts/validate_windows_bundle.py --dist $bundle --zip $zip
    if ($LASTEXITCODE -ne 0) { throw "Состав ZIP не соответствует проверенной папке onedir." }

    # Test the actual *delivered ZIP*, not only PyInstaller's working directory.
    # Extraction into a Unicode path also catches encoding/path packaging bugs.
    $extractSandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("kontakt-release-" + [guid]::NewGuid().ToString("N"))
    $extractRoot = Join-Path $extractSandbox "Путь с пробелами и кириллицей"
    try {
        New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
        Expand-Archive -LiteralPath $zip -DestinationPath $extractRoot -Force
        $fromZip = Join-Path $extractRoot "kontakt"
        if (-not (Test-Path -LiteralPath (Join-Path $fromZip "kontakt.exe") -PathType Leaf)) {
            throw "После распаковки ZIP ожидается katalog kontakt с kontakt.exe."
        }
        & $pythonExe scripts/check_windows_bundle.py --dist $fromZip
        if ($LASTEXITCODE -ne 0) { throw "Распакованный ZIP не прошёл тест запуска EXE." }
    } finally {
        if (Test-Path -LiteralPath $extractSandbox) {
            Remove-Item -LiteralPath $extractSandbox -Recurse -Force
        }
    }

    & $pythonExe -m pip freeze | Out-File -FilePath "dist\build-requirements.txt" -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "Не удалось сохранить версии зависимостей." }
    # A PR workflow normally checks out a synthetic merge commit, not PR HEAD.
    $dirty = @(& git status --porcelain --untracked-files=normal)
    if ($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) {
        throw "Git worktree changed during build: $($dirty -join '; ')"
    }
    $currentCheckout = (& git rev-parse HEAD).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $currentCheckout -ne $buildCheckoutCommit) {
        throw "Git checkout changed during build."
    }
    $sha = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToUpperInvariant()
    $manifestFile = Join-Path $root "dist\release-manifest.json"
    [ordered]@{
        format_version = 2
        build_checkout_commit = $buildCheckoutCommit
        build_tree_sha = $buildTreeSha
        pr_head_commit = $(if ($prHeadCommit) { $prHeadCommit } else { $null })
        zip_filename = "kontakt-windows-x64.zip"
        zip_sha256 = $sha
        os_used_for_build = [System.Environment]::OSVersion.VersionString
        created_utc = [DateTime]::UtcNow.ToString("o")
        acceptance = "release-candidate-not-windows-10-11-certified"
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $manifestFile -Encoding utf8
    $checkArgs = @("--manifest", $manifestFile, "--zip", $zip,
                   "--build-checkout-commit", $buildCheckoutCommit, "--build-tree-sha", $buildTreeSha)
    if ($prHeadCommit) { $checkArgs += @("--pr-head-commit", $prHeadCommit) }
    & $pythonExe scripts/check_release_manifest.py @checkArgs
    if ($LASTEXITCODE -ne 0) { throw "Manifest does not match checkout/tree/archive." }
    Get-FileHash -LiteralPath $zip -Algorithm SHA256 | Format-List
    Write-Host "Манифест SHA-256: $manifestFile"
    Write-Host "Готовая поставка: $zip"
} finally {
    Pop-Location
}

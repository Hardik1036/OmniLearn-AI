<#
.SYNOPSIS
    Builds the OmniLearn AI Desktop System App (Windows x64) via PyInstaller
    and optionally compiles the Inno Setup installer.
.DESCRIPTION
    Bundles local_app/app_launcher.py together with static frontend assets
    (public/) and SQLite database schemas/seeds (database/) into a standalone
    Windows desktop application directory under dist\OmniLearn\, computes SHA-256
    checksums, and invokes ISCC.exe when available.
#>

[CmdletBinding()]
param(
    [string]$Version = "1.0.0",
    [string]$PythonExe = "python",
    [switch]$OneFile,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$DistDir = Join-Path $RepoRoot "dist"
$BuildDir = Join-Path $RepoRoot "build"

Write-Host "==> OmniLearn AI System App Build (v$Version)" -ForegroundColor Cyan
Write-Host "    Repository Root : $RepoRoot"

Push-Location $RepoRoot
try {
    # 1. Verify required repository paths exist
    foreach ($reqPath in @("backend\local_app\app_launcher.py", "frontend", "database\schema.sql", "database\local_schema.sql", "database\seed.sql")) {
        if (-not (Test-Path (Join-Path $RepoRoot $reqPath))) {
            throw "Required build input missing: $reqPath"
        }
    }

    # 2. Check PyInstaller availability
    & $PythonExe -m PyInstaller --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed in '$PythonExe'. Run: $PythonExe -m pip install pyinstaller"
    }

    # 3. Clean previous output artifacts
    foreach ($dir in @((Join-Path $DistDir "OmniLearn"), (Join-Path $BuildDir "OmniLearn"))) {
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir
        }
    }

    # 4. Construct PyInstaller arguments
    $ModeFlag = if ($OneFile) { "--onefile" } else { "--onedir" }

    $PyInstallerArgs = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        $ModeFlag,
        "--name", "OmniLearn",
        "--paths", $RepoRoot,
        "--add-data", "frontend;frontend",
        "--add-data", "database;database",
        "--hidden-import", "backend",
        "--hidden-import", "backend.local_app",
        "--hidden-import", "backend.local_app.local_api",
        "--hidden-import", "backend.local_app.local_db",
        "--hidden-import", "backend.local_app.gemini_local",
        "--hidden-import", "backend.local_app.source_search",
        "--hidden-import", "backend.local_app.sync_client",
        "--hidden-import", "backend.local_app.parsers",
        "--hidden-import", "backend.local_app.parsers.doc_parsers",
        "--hidden-import", "backend.local_app.parsers.web_crawler",
        "--hidden-import", "backend.local_app.parsers.youtube_parser",
        "--hidden-import", "backend.local_app.parsers.video_parser",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "fastapi",
        "--hidden-import", "starlette",
        "--hidden-import", "multipart",
        "--hidden-import", "python_multipart",
        "--hidden-import", "pydantic",
        "--hidden-import", "httpx",
        "--hidden-import", "fitz",
        "--hidden-import", "openpyxl",
        "--hidden-import", "pptx",
        "--hidden-import", "trafilatura",
        "--hidden-import", "bs4",
        "--hidden-import", "charset_normalizer",
        "--hidden-import", "youtube_transcript_api",
        "--hidden-import", "yt_dlp",
        "--hidden-import", "cv2",
        "--hidden-import", "numpy",
        "--hidden-import", "PIL",
        "--hidden-import", "keyring",
        "--hidden-import", "keyring.backends.Windows",
        "--hidden-import", "cryptography",
        "--hidden-import", "webview",
        "--collect-submodules", "webview",
        "--collect-submodules", "trafilatura",
        "backend/local_app/app_launcher.py"
    )

    $IconPath = Join-Path $RepoRoot "frontend\favicon.ico"
    if (Test-Path $IconPath) {
        $PyInstallerArgs = @($PyInstallerArgs[0..5]) + @("--icon", $IconPath) + @($PyInstallerArgs[6..($PyInstallerArgs.Count - 1)])
    }

    Write-Host "==> Running PyInstaller ($ModeFlag)..." -ForegroundColor Yellow
    & $PythonExe @PyInstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }

    # 5. Zip portable distribution & emit SHA-256 checksum
    $PortableZip = Join-Path $DistDir "OmniLearn-Windows-x64-v$Version.zip"
    if ($OneFile) {
        $ExePath = Join-Path $DistDir "OmniLearn.exe"
        $Hash = (Get-FileHash -Path $ExePath -Algorithm SHA256).Hash.ToLower()
        "$Hash  OmniLearn.exe" | Set-Content -Path (Join-Path $DistDir "OmniLearn.exe.sha256") -Encoding UTF8
        Write-Host "==> Built standalone executable: $ExePath ($Hash)" -ForegroundColor Green
    } else {
        $BundleDir = Join-Path $DistDir "OmniLearn"
        if (Test-Path $PortableZip) {
            Remove-Item -Force $PortableZip
        }
        Compress-Archive -Path "$BundleDir\*" -DestinationPath $PortableZip -CompressionLevel Optimal
        $ZipHash = (Get-FileHash -Path $PortableZip -Algorithm SHA256).Hash.ToLower()
        $ExeHash = (Get-FileHash -Path (Join-Path $BundleDir "OmniLearn.exe") -Algorithm SHA256).Hash.ToLower()
        "$ZipHash  $(Split-Path -Leaf $PortableZip)" | Set-Content -Path "$PortableZip.sha256" -Encoding UTF8
        Write-Host "==> Built portable bundle: $BundleDir (OmniLearn.exe SHA-256: $ExeHash)" -ForegroundColor Green
        Write-Host "==> Built portable zip   : $PortableZip (SHA-256: $ZipHash)" -ForegroundColor Green
    }

    # 6. Build Inno Setup installer if ISCC.exe is installed and not skipped
    if (-not $SkipInstaller -and -not $OneFile) {
        $IssPath = Join-Path $ScriptDir "installer.iss"
        $IsccCandidates = @(
            (Get-Command "ISCC.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        ) | Where-Object { $_ -and (Test-Path $_) }

        if ($IsccCandidates.Count -gt 0) {
            $IsccExe = $IsccCandidates[0]
            Write-Host "==> Running Inno Setup Compiler: $IsccExe" -ForegroundColor Yellow
            & $IsccExe "/DMyAppVersion=$Version" $IssPath
            if ($LASTEXITCODE -ne 0) {
                throw "Inno Setup compiler failed with exit code $LASTEXITCODE"
            }
            $SetupExe = Join-Path $DistDir "OmniLearn-Setup-$Version.exe"
            if (Test-Path $SetupExe) {
                $SetupHash = (Get-FileHash -Path $SetupExe -Algorithm SHA256).Hash.ToLower()
                "$SetupHash  $(Split-Path -Leaf $SetupExe)" | Set-Content -Path "$SetupExe.sha256" -Encoding UTF8
                Write-Host "==> Built Windows Installer: $SetupExe (SHA-256: $SetupHash)" -ForegroundColor Green
            }
        } else {
            Write-Host "==> Inno Setup (ISCC.exe) not detected; skipping .exe installer creation." -ForegroundColor DarkYellow
            Write-Host "    Install Inno Setup 6 and run: ISCC.exe packaging\installer.iss" -ForegroundColor DarkGray
        }
    }
}
finally {
    Pop-Location
}

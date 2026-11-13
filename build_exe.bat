@echo off
cd /d "%~dp0"
echo ========================================================
echo Membangun Standalone Executable (.exe) dengan PyInstaller
echo ========================================================
echo.

python -m pip install --quiet pyinstaller
if errorlevel 1 (
    echo [ERROR] Gagal menginstall / memverifikasi PyInstaller.
    pause
    exit /b 1
)

echo Menjalankan PyInstaller dengan spec file (termasuk DLL kompatibilitas Windows 7/Server 2008)...
pyinstaller --clean --noconfirm SGAbsensiSimulator.spec

if errorlevel 1 (
    echo.
    echo [ERROR] Build gagal!
    pause
    exit /b 1
)

copy /y "compat\x64\api-ms-win-core-path-l1-1-0.dll" "dist\" >nul

echo.
echo ========================================================
echo Build Berhasil!
echo File executable berada di: dist\SGAbsensiSimulator.exe
echo Anda dapat menyalin file tersebut ke komputer lain tanpa perlu install Python.
echo ========================================================
pause

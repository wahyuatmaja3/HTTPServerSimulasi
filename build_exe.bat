@echo off
cd /d "%~dp0"
echo ========================================================
echo Membangun Standalone Executable (.exe) dengan PyInstaller
echo ========================================================
echo.

set PYTHON_CMD=python
py -3.8 --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON_CMD=py -3.8
    echo Menggunakan Python 3.8 untuk kompatibilitas Windows Server 2008 / Windows 7...
) else (
    echo Menggunakan default python environment...
)

%PYTHON_CMD% -m pip install --quiet pyinstaller
if errorlevel 1 (
    echo [ERROR] Gagal menginstall / memverifikasi PyInstaller.
    pause
    exit /b 1
)

echo Menjalankan PyInstaller dengan spec file...
%PYTHON_CMD% -m PyInstaller --clean --noconfirm SGAbsensiSimulator.spec

if errorlevel 1 (
    echo.
    echo [ERROR] Build gagal!
    pause
    exit /b 1
)

if exist "compat\x64\api-ms-win-core-path-l1-1-0.dll" (
    copy /y "compat\x64\api-ms-win-core-path-l1-1-0.dll" "dist\" >nul
)

echo.
echo ========================================================
echo Build Berhasil!
echo File executable berada di: dist\SGAbsensiSimulator.exe
echo Anda dapat menyalin file tersebut ke komputer lain tanpa perlu install Python.
echo ========================================================
pause

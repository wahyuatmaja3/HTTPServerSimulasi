@echo off
cd /d "%~dp0"
python main.py
if errorlevel 1 (
    echo.
    echo [ERROR] Gagal menjalankan SG Absensi Simulator. Pastikan Python dan library di requirements.txt terinstall.
    pause
)

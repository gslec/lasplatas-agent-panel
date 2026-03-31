@echo off
title LasPlatas - Panel Web
cd /d "%~dp0"

echo Verificando Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python no esta instalado o no esta en el PATH.
    pause
    exit /b
)

echo Instalando dependencias...
pip install requests -q

echo.
echo =========================================
echo   Iniciando panel web de LasPlatas...
echo =========================================
echo.
echo Abre esta direccion en tu navegador:
echo http://127.0.0.1:8000
echo.

python lasplatas_web.py
if errorlevel 1 (
    echo.
    echo El servidor se cerro por un error.
    pause
)

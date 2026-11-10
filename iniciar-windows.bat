@echo off
rem Roda o conversor direto no Windows (sem Docker).
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Criando ambiente Python e instalando dependencias...
    python -m venv .venv || goto erro
    .venv\Scripts\python -m pip install -r requirements.txt || goto erro
)

if not exist config\config.yaml (
    echo Crie config\config.yaml a partir de config\config.example.yaml
    pause
    exit /b 1
)

.venv\Scripts\python -m netip_bridge run --config config\config.yaml
pause
exit /b 0

:erro
echo Falha ao preparar o ambiente Python.
pause
exit /b 1

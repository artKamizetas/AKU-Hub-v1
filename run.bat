@echo off
echo ========================================
echo  AKU Dashboard - Streamlit
echo ========================================
echo.

REM Verifica se o venv existe
if not exist "venv" (
    echo [ERRO] Ambiente virtual nao encontrado!
    echo Crie o ambiente com: python -m venv venv
    echo E instale as dependencias: venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

REM Ativa o ambiente virtual
call venv\Scripts\activate.bat

REM Verifica se o app existe
if not exist "app.py" (
    echo [ERRO] Arquivo 'app.py' nao encontrado!
    pause
    exit /b 1
)

echo Iniciando o dashboard...
echo.
streamlit run app.py

echo.
echo ========================================
echo  DASHBOARD ENCERRADO
echo ========================================
echo.
pause

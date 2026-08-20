@echo off
setlocal
cd /d "%~dp0"
title Gerar Baixador NFSe

echo Preparando o Baixador NFSe com icone personalizado...
where py >nul 2>nul
if errorlevel 1 (
  echo.
  echo O Python nao foi encontrado neste computador.
  echo Execute este arquivo no notebook em que o programa ja foi testado.
  echo.
  pause
  exit /b 1
)

py -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 goto :erro

py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "Baixador NFSe" ^
  --icon "icone_nfse.ico" ^
  app.py
if errorlevel 1 goto :erro

copy /y "dist\Baixador NFSe.exe" "%~dp0Baixador NFSe.exe" >nul
echo.
echo PRONTO.
echo O arquivo "Baixador NFSe.exe" foi criado nesta mesma pasta.
echo O executavel ja inclui o icone oficial do aplicativo.
echo Ele pode ser copiado para outros computadores Windows e aberto com dois cliques.
echo.
explorer /select,"%~dp0Baixador NFSe.exe"
pause
exit /b 0

:erro
echo.
echo Nao foi possivel gerar o aplicativo. Tire uma foto desta tela e envie.
echo.
pause
exit /b 1

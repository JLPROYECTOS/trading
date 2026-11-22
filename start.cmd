@echo off
title Senales de Trading
cd /d "%~dp0"
echo Arrancando la app de senales en http://127.0.0.1:8765 ...
echo Deja esta ventana abierta para recibir senales. Ctrl+C para parar.
python "%~dp0app\server.py"
pause

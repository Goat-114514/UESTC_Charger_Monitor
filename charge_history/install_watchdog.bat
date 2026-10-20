@echo off
chcp 65001 >nul
set PY=C:\Users\86185\AppData\Local\Programs\Python\Python314\pythonw.exe
set WD=C:\Users\86185\Desktop\UESTC_Charger_Monitor-master\charge_history\watchdog.py

echo Installing ChargerWatchdog (every 5 minutes)...
schtasks /Create /TN "ChargerWatchdog" /TR "%PY% %WD% --once" /SC MINUTE /MO 5 /F
if errorlevel 1 (
    echo.
    echo FAILED. Right-click this file and choose "Run as administrator", then retry.
    pause
    exit /b 1
)

echo.
echo Installed. Current status:
schtasks /Query /TN "ChargerWatchdog" /FO LIST
echo.
echo Test run now:
schtasks /Run /TN "ChargerWatchdog"
timeout /t 8 >nul
echo Check log: %~dp0logs\watchdog.log
pause

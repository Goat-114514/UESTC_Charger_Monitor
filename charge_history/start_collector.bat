@echo off
cd /d "%~dp0"
"C:\Users\86185\AppData\Local\Programs\Python\Python314\python.exe" -u charge_history_backend.py --db charge_data\charge_20261020_175905.db >> "logs\collector.log" 2>&1

@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python -m agh_lite ui
if errorlevel 1 (
  echo.
  echo 启动失败。先确认已安装：pip install -e . 或 pip install agh-lite
  pause
)

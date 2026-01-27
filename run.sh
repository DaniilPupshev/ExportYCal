#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f ".env" ]]; then
  echo "[ERROR] Не найден файл .env"
  echo "Скопируй .env.example в .env и заполни переменные."
  exit 1
fi

if [[ ! -f "requirements.txt" ]]; then
  echo "[ERROR] Не найден requirements.txt"
  exit 1
fi

if ! command -v python >/dev/null 2>&1; then
  echo "[ERROR] Не найден python. Установи Python 3 и попробуй снова."
  exit 1
fi

echo "[INFO] Устанавливаю зависимости..."
python -m pip install -r requirements.txt

echo "[INFO] Запускаю скрипт (с записью в календарь)..."
RASP_DRY_RUN=0 python main.py

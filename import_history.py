"""
Прямий імпорт експорту Telegram Desktop у базу даних бота.
Запускати на сервері (або локально), де лежить result.json.

Використання:
    python import_history.py result.json <chat_id>

Приклад:
    python import_history.py /home/user/exports/result.json -1001234567890

chat_id можна дізнатись через @username_to_id_bot або з посилання на чат.
"""

import sys
import json
import os
import time

# Підключаємо ту саму базу що й бот
os.environ.setdefault("DB_PATH", "bot.db")
import database as db


def human_size(path: str) -> str:
    size = os.path.getsize(path)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    json_path = sys.argv[1]
    try:
        chat_id = int(sys.argv[2])
    except ValueError:
        print(f"❌ Невірний chat_id: {sys.argv[2]}")
        sys.exit(1)

    if not os.path.exists(json_path):
        print(f"❌ Файл не знайдено: {json_path}")
        sys.exit(1)

    print(f"📂 Файл:   {json_path} ({human_size(json_path)})")
    print(f"💬 Chat ID: {chat_id}")
    print("⏳ Читаю файл...")

    t0 = time.time()
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    total_in_file = len(data.get("messages", []))
    print(f"📨 Повідомлень у файлі: {total_in_file:,}")
    print("⏳ Імпортую в базу даних...")

    db.init_db()
    result = db.import_from_telegram_export(chat_id, data)

    elapsed = time.time() - t0
    print()
    print("✅ Готово!")
    print(f"   Повідомлень імпортовано : {result['messages']:,}")
    print(f"   Унікальних користувачів : {result['users']:,}")
    print(f"   Час виконання           : {elapsed:.1f} сек")
    print()
    print("Тепер у боті доступні команди !вся стата і !моя стата з повною історією.")


if __name__ == "__main__":
    main()
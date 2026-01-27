import os
import json
import re
import uuid
import time
import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # Если python-dotenv не установлен, просто продолжаем без .env
    pass
from datetime import datetime, timedelta
from dateutil import tz, parser as dtparser

BASE = "https://rasp.dmami.ru"
TZ = tz.gettz("Europe/Moscow")

GROUP = os.environ.get("RASP_GROUP", "241-161")
SESSION = os.environ.get("RASP_SESSION", "0")

# Время пар задаём явно (можно переопределить через env RASP_PAIR_TIMES)
# Формат env: "1=09:00,2=10:40,3=12:20,4=14:30,5=16:10"
PAIR_TIMES_ENV = os.environ.get("RASP_PAIR_TIMES", "")
PAIR_DURATION_MIN = int(os.environ.get("RASP_PAIR_DURATION_MIN", "90"))
DEFAULT_PAIR_START_TIMES = {
    "1": "09:00",
    "2": "10:40",
    "3": "12:20",
    "4": "14:30",
    "5": "16:10",
}
ROOM_EXCLUDE = {"_Спортзал_", "________ПД________", "_*ПД*_"}

# Лучше хранить логин/пароль в переменных окружения, а не в коде:
# export YANDEX_LOGIN="your_login@yandex.ru"
# export YANDEX_PASSWORD="your_app_password"
YANDEX_LOGIN = os.environ.get("YANDEX_LOGIN")
YANDEX_PASSWORD = os.environ.get("YANDEX_PASSWORD")

DUMP_FILE = "rasp_raw.json"
DRY_RUN = os.environ.get("RASP_DRY_RUN", "1") != "0"


def parse_pair_times(raw: str):
    if not raw:
        return {}
    out = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        k, v = part.split("=", 1)
        out[str(int(k))] = v.strip()
    return out


PAIR_START_TIMES = parse_pair_times(PAIR_TIMES_ENV) or DEFAULT_PAIR_START_TIMES


def fetch_group_payload(group: str, session: str) -> dict:
    # Рабочий endpoint (с нужными заголовками): /site/group?group=...&session=...
    url = f"{BASE}/site/group"
    params = {"group": group, "session": session}

    # Логируем, чтобы точно видеть, куда уходит запрос
    print("[HTTP] GET", url, params)

    r = requests.get(
        url,
        params=params,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (ExpYC; python-requests)",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/",
        },
    )

    print("[HTTP] FINAL URL:", r.url)
    print("[HTTP] STATUS:", r.status_code)

    # Если вдруг снова 404 — покажем первые символы ответа для диагностики
    if r.status_code == 404:
        print("[HTTP] 404 body (first 200 chars):", r.text[:200])

    r.raise_for_status()
    return r.json()


def save_dump(payload: dict, path: str = DUMP_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[OK] Сохранил сырой ответ в {path}")


def _walk(obj):
    """Обход всех словарей/списков в JSON."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk(x)


def parse_lessons(payload: dict):
    """
    Попытка автоматически достать занятия из JSON.
    Если что — ниже будет место, где проще всего вручную подстроить.
    """
    # 0) Явная структура grid (как в rasp_raw.json)
    grid = payload.get("grid")
    if isinstance(grid, dict):
        if not PAIR_START_TIMES:
            raise RuntimeError(
                "В rasp_raw.json нет времени пар. Задай RASP_PAIR_TIMES, например: "
                "RASP_PAIR_TIMES='1=09:00,2=10:40,3=12:20,4=14:00,5=15:40,6=17:20,7=19:00'"
            )
        lessons = []
        for day_key, pairs in grid.items():
            if not isinstance(pairs, dict):
                continue
            weekday = int(day_key)  # 1..6 (пн..сб)
            for pair_key, items in pairs.items():
                if not isinstance(items, list):
                    continue
                start_time = PAIR_START_TIMES.get(str(pair_key))
                if not start_time:
                    continue
                for d in items:
                    if not isinstance(d, dict):
                        continue
                    title = (d.get("sbj") or "").strip()
                    if not title:
                        continue
                    df = d.get("df")
                    dt = d.get("dt")
                    if not df or not dt:
                        continue
                    try:
                        start_date = dtparser.parse(df).date()
                        end_date = dtparser.parse(dt).date()
                    except Exception:
                        continue

                    # первая дата, совпадающая с нужным днем недели (1=пн)
                    first_date = start_date
                    while first_date.isoweekday() != weekday:
                        first_date += timedelta(days=1)
                        if first_date > end_date:
                            break
                    if first_date > end_date:
                        continue

                    # последняя дата для UNTIL
                    last_date = end_date
                    while last_date.isoweekday() != weekday:
                        last_date -= timedelta(days=1)
                        if last_date < start_date:
                            break
                    if last_date < start_date:
                        continue

                    start_dt = dtparser.parse(f"{first_date} {start_time}").replace(tzinfo=TZ)
                    end_dt = start_dt + timedelta(minutes=PAIR_DURATION_MIN)
                    until_dt = dtparser.parse(f"{last_date} {start_time}").replace(tzinfo=TZ) + timedelta(
                        minutes=PAIR_DURATION_MIN
                    )

                    # аудитории
                    rooms = []
                    for r in d.get("shortRooms") or []:
                        if r and r not in rooms:
                            rooms.append(r)
                    if not rooms:
                        for a in d.get("auditories") or []:
                            if isinstance(a, dict) and a.get("title") and a["title"] not in rooms:
                                rooms.append(a["title"])
                    room_text = ", ".join(rooms) if rooms else None
                    if room_text and any(r in ROOM_EXCLUDE for r in rooms):
                        continue

                    lesson_type = d.get("type") or ""

                    desc_parts = []
                    if room_text:
                        desc_parts.append(f"Аудитория: {room_text}")
                    if lesson_type:
                        desc_parts.append(f"Тип: {lesson_type}")
                    description = "\n".join(desc_parts) if desc_parts else None

                    stable_key = f"{GROUP}|{title}|{weekday}|{pair_key}|{df}|{dt}|{room_text or ''}|{lesson_type}"
                    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, stable_key))

                    lessons.append(
                        {
                            "uid": uid,
                            "title": title,
                            "start": start_dt,
                            "end": end_dt,
                            "location": room_text,
                            "description": description,
                            "rrule": {"freq": "weekly", "until": until_dt},
                        }
                    )

        # Уберём дубликаты по uid
        uniq = {x["uid"]: x for x in lessons}
        lessons = list(uniq.values())
        lessons.sort(key=lambda x: x["start"])
        return lessons

    lessons = []

    # 1) Сначала ищем "похожие на занятие" словари:
    # есть (дата/время) + (название/дисциплина) + (аудитория/препод)
    for d in _walk(payload):
        keys = {k.lower() for k in d.keys()}

        # типичные ключи (могут отличаться — это эвристика)
        has_title = any(k in keys for k in ["subject", "title", "discipline", "name"])
        has_time = any(k in keys for k in ["start", "end", "time_start", "timeend", "time_end", "begin", "finish"])
        has_date = any(k in keys for k in ["date", "day", "datetime", "startdate"])

        if not (has_title and (has_time or has_date)):
            continue

        # 2) Извлекаем поля (попробуем несколько вариантов)
        title = (d.get("subject") or d.get("title") or d.get("discipline") or d.get("name") or "").strip()
        if not title:
            continue

        # время/дата могут быть в разных форматах
        # варианты: start/end как ISO, либо date + time_start/time_end
        start_raw = d.get("start") or d.get("begin") or d.get("time_start")
        end_raw = d.get("end") or d.get("finish") or d.get("time_end")

        date_raw = d.get("date") or d.get("day") or d.get("startDate") or d.get("startdate")

        def to_dt(x):
            if x is None:
                return None
            if isinstance(x, (int, float)):
                # иногда миллисекунды/секунды от эпохи
                if x > 10_000_000_000:
                    return datetime.fromtimestamp(x / 1000, TZ)
                return datetime.fromtimestamp(x, TZ)
            if isinstance(x, str):
                return dtparser.parse(x).astimezone(TZ) if ("+" in x or "Z" in x) else dtparser.parse(x).replace(tzinfo=TZ)
            return None

        start_dt = to_dt(start_raw)
        end_dt = to_dt(end_raw)

        # если нет start/end, но есть date + time_start/time_end
        if (start_dt is None or end_dt is None) and date_raw and isinstance(date_raw, str):
            try:
                if isinstance(start_raw, str) and isinstance(end_raw, str):
                    start_dt = dtparser.parse(f"{date_raw} {start_raw}").replace(tzinfo=TZ)
                    end_dt = dtparser.parse(f"{date_raw} {end_raw}").replace(tzinfo=TZ)
            except Exception:
                pass

        if not start_dt or not end_dt:
            continue

        teacher = (d.get("teacher") or d.get("lecturer") or d.get("tutor") or "").strip()
        room = (d.get("room") or d.get("auditorium") or d.get("aud") or "").strip()
        building = (d.get("building") or d.get("campus") or "").strip()

        location = ", ".join([x for x in [building, room] if x]).strip() or None

        desc_parts = []
        if teacher:
            desc_parts.append(f"Преподаватель: {teacher}")
        if d.get("type"):
            desc_parts.append(f"Тип: {d.get('type')}")
        if d.get("group"):
            desc_parts.append(f"Группа: {d.get('group')}")
        description = "\n".join(desc_parts) if desc_parts else None

        # Стабильный UID чтобы не плодить дубли при повторном запуске
        stable_key = f"{GROUP}|{title}|{start_dt.isoformat()}|{end_dt.isoformat()}|{location or ''}"
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, stable_key))

        lessons.append({
            "uid": uid,
            "title": title,
            "start": start_dt,
            "end": end_dt,
            "location": location,
            "description": description
        })

    # Уберём дубликаты по uid
    uniq = {}
    for x in lessons:
        uniq[x["uid"]] = x
    lessons = list(uniq.values())
    lessons.sort(key=lambda x: x["start"])
    return lessons


def get_calendar():
    import caldav

    if not YANDEX_LOGIN or not YANDEX_PASSWORD:
        raise RuntimeError("Нужно задать YANDEX_LOGIN и YANDEX_PASSWORD (лучше пароль приложения).")

    yandex_url = os.environ.get("YANDEX_URL")
    if not yandex_url:
        raise RuntimeError("Нужно задать YANDEX_URL (например, CalDAV адрес Яндекса).")

    client = caldav.DAVClient(url=yandex_url, username=YANDEX_LOGIN, password=YANDEX_PASSWORD)
    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise RuntimeError("Не найдено ни одного календаря в Яндексе по CalDAV.")
    # Ищем календарь по имени
    target_name = os.environ.get("YANDEX_CALENDAR_NAME", "Расписание Уник")
    for cal in calendars:
        try:
            name = cal.get_properties(["{DAV:}displayname"]).get("{DAV:}displayname")
            if name == target_name:
                return cal
        except Exception:
            pass
        if getattr(cal, "name", None) == target_name:
            return cal
    # fallback: первый календарь
    return calendars[0]


def upsert_event(calendar, ev):
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//RASP DMAMI Sync//")
    cal.add("version", "2.0")

    e = Event()
    e.add("uid", ev["uid"])
    e.add("summary", ev["title"])
    e.add("dtstart", ev["start"])
    e.add("dtend", ev["end"])
    if ev.get("rrule"):
        e.add("rrule", ev["rrule"])
    if ev["location"]:
        e.add("location", ev["location"])
    if ev["description"]:
        e.add("description", ev["description"])

    cal.add_component(e)
    ics = cal.to_ical()

    # фиксированный href = uid.ics (удобно для “обновлять тем же именем”)
    href = f"{ev['uid']}.ics"
    calendar.add_event(ics, href=href)


def main():
    payload = fetch_group_payload(GROUP, SESSION)
    print("[DEBUG] payload type:", type(payload))
    save_dump(payload)

    lessons = parse_lessons(payload)
    print(f"[OK] Нашёл занятий: {len(lessons)}")
    if lessons:
        print("[Пример] Первое:", lessons[0]["start"], "-", lessons[0]["title"])

    # Если занятий 0 — значит структура другая, и надо настроить parse_lessons
    if not lessons:
        print("\n[!] Не удалось автоматически распарсить занятия.")
        print("Открой rasp_raw.json и найди, где лежат пары (по словам subject/title/time/date).")
        print("Потом подстроим parse_lessons под реальные ключи.")
        return

    if DRY_RUN:
        print("[OK] DRY_RUN=1 — календарь не трогаю. Посмотри rasp_raw.json и, при желании, распарсенный список.")
        print("[Пример] Первые 3 занятия:")
        for ev in lessons[:3]:
            print(" -", ev["start"], "-", ev["end"], "-", ev["title"])
        return

    cal = get_calendar()

    # Чтобы не словить таймаут — небольшая пауза между PUT
    for i, ev in enumerate(lessons, 1):
        upsert_event(cal, ev)
        if i % 10 == 0:
            time.sleep(1)

    print(f"[OK] Загружено в Яндекс.Календарь событий: {len(lessons)}")


if __name__ == "__main__":
    main()

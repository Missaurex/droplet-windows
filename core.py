"""Логика без Windows: номер, поиск карточки в amoCRM, текст примечания. Проверяется тестами на любом компьютере."""
import json
import re

FIELDS = [("город", "Город"), ("цель", "Цель"), ("для_кого", "Для кого"), ("прайс", "Озвученный прайс"),
          ("личное", "Личные детали клиента"), ("договорённости", "Договорённости")]

BROWSERS = ("chrome.exe", "msedge.exe", "yandex", "browser.exe", "firefox.exe", "opera")
MESSENGERS = ("whatsapp", "telegram")
_INVISIBLE = dict.fromkeys([0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E])
_DASHES = {c: "-" for c in range(0x2010, 0x2016)}
_PHONE = re.compile(r"\+?\d[\d\s\-()]{8,16}\d")


def call_app(mic_users, own):
    """Какое приложение звонит: первое, кто держит микрофон, кроме нас. Возвращает (имя, 'browser'|'messenger') или None."""
    for name in mic_users:
        n = name.lower()
        if own and own.lower() in n:
            continue
        if any(m in n for m in MESSENGERS):
            return n, "messenger"
        if any(b in n for b in BROWSERS):
            return n, "browser"
    return None


def _clean(text):
    return text.translate(_INVISIBLE).translate(_DASHES)


def _format(raw):
    d = "".join(ch for ch in raw if ch.isdigit())
    if len(d) == 10:
        d = "7" + d
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if not 10 <= len(d) <= 15:
        return None
    if len(d) == 11 and d[0] == "7":
        return f"+7 {d[1:4]} {d[4:7]} {d[7:]}"
    return "+" + d


def phone(text):
    """Первый номер в тексте → «+7 777 777 7777» (без тире)."""
    m = _PHONE.search(_clean(text or ""))
    return _format(m.group()) if m else None


def call_phone(context):
    """Номер собеседника из окна звонка.
    Строка со словом «звонок/call» и номером — берём его. Иначе только если в окне ровно один номер:
    два разных номера — не угадываем."""
    parts = (context or "").split(" | ")
    for p in parts:
        if any(w in p.lower() for w in ("call", "звонок", "вызов")):
            ph = phone(p)
            if ph:
                return ph
    found = {_format(m.group()) for m in _PHONE.finditer(_clean(context or ""))} - {None}
    return found.pop() if len(found) == 1 else None


def card(url):
    """«https://co.amocrm.ru/leads/detail/123» → ('leads', 123)."""
    m = re.search(r"(amocrm\.(ru|com)|kommo\.com)/(leads|contacts|companies|customers)/detail/(\d+)", url or "")
    return (m.group(3), int(m.group(4))) if m else None


def phones(contact):
    """Последние 10 цифр каждого телефона контакта amoCRM."""
    out = []
    for f in contact.get("custom_fields_values") or []:
        if f.get("field_code") == "PHONE":
            for v in f.get("values") or []:
                out.append("".join(c for c in str(v.get("value", "")) if c.isdigit())[-10:])
    return out


def find(request, ph):
    """Клиент по номеру. request(path) → (код, текст). Ровно один контакт с этим номером — его самая новая сделка
    (или сам контакт). Два контакта с одним номером или ни одного — None: чужое не пишем."""
    digits = "".join(c for c in ph if c.isdigit())
    from urllib.parse import quote
    for q in (ph, digits, digits[-10:]):
        code, text = request(f"/api/v4/contacts?with=leads&limit=50&query={quote(q)}")
        if code != 200:
            continue
        try:
            contacts = json.loads(text).get("_embedded", {}).get("contacts", [])
        except ValueError:
            continue
        same = [c for c in contacts if digits[-10:] in phones(c)]
        if len(same) > 1:
            return None
        if same:
            c = same[0]
            leads = [l["id"] for l in (c.get("_embedded") or {}).get("leads", []) if "id" in l]
            return ("leads", max(leads)) if leads else ("contacts", c["id"])
    return None


def summary_json(r):
    """JSON из ответа Claude, даже если вокруг лишний текст."""
    a, b = (r or "").find("{"), (r or "").rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        j = json.loads(r[a:b + 1])
    except ValueError:
        return {}
    return {k: ("; ".join(v) if isinstance(v, list) else str(v)) for k, v in j.items() if isinstance(v, (str, list))}


def field(r, k):
    v = summary_json(r).get(k, "").strip()
    return None if v in ("", "—") else v


def prompt(dialog, context):
    lines = "\n".join(f"{who}: {text}" for who, text in dialog)[-30000:]
    return f"""Расшифровка звонка менеджера («МОП») с клиентом. Распознано автоматически, с ошибками — понимай по смыслу.
Текст окна звонка (там может быть имя или номер клиента): {context or "—"}

{lines}

Верни ТОЛЬКО JSON, без пояснений и markdown:
{{"имя": "", "телефон": "", "город": "", "цель": "", "для_кого": "", "прайс": "", "личное": "", "договорённости": ""}}
цель — зачем обратился; для_кого — для кого заказ; прайс — какие цены прозвучали; личное — личные детали клиента \
(повод, семья, даты, пожелания); договорённости — что решили и следующий шаг. Коротко, только факты из разговора. \
Чего не было — "—". Ничего не выдумывай."""


def note_text(r, minutes, dialog):
    """Примечание: сначала ключевое, потом весь диалог."""
    rows = [f"{t}: {field(r, k)}" for k, t in FIELDS if field(r, k)]
    t = f"Итог звонка, {minutes} мин (Droplet)\n\nКЛЮЧЕВОЕ\n" + ("\n".join(rows) if summary_json(r) else (r or "—"))
    if dialog:
        # shortcut: режем очень длинный диалог до 20 000 знаков; если amoCRM примет больше — поднять
        t += "\n\nВЕСЬ ДИАЛОГ\n" + "\n".join(f"{who}: {text}" for who, text in dialog)[:20000]
    return t


def merge(me, them):
    """Две дорожки [(секунда, текст)] → один диалог по времени."""
    both = [(s, "МОП", t) for s, t in me] + [(s, "Клиент", t) for s, t in them]
    out = []
    for _, who, text in sorted(both, key=lambda x: x[0]):
        if out and out[-1][0] == who:
            out[-1] = (who, out[-1][1] + " " + text)
        else:
            out.append((who, text))
    return out


def track_card(state, seen):
    """Sipuni: запоминаем первую сделку звонка; открыли другую — звонок помечаем как «непонятно чей»."""
    if not seen:
        return
    if state.get("card") is None:
        state["card"] = seen
    elif state["card"] != seen:
        state["changed"] = True

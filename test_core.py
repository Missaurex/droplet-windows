"""python3 test_core.py — проверка логики без Windows."""
import json
import core

WA = "‎‪+7 968 609‑71‑19‬ - WhatsApp voice call | ‪+7 968 609‑71‑19‬ | mute off"
assert core.phone("8 (777) 123 45 67") == "+7 777 123 4567"
assert core.phone("+996 555 123 456") == "+996555123456" and core.phone("Аня, 12:30") is None
assert core.call_phone(WA) == "+7 968 609 7119"
assert core.call_phone("Аня | +7 777 111 2233 | Голосовой вызов") == "+7 777 111 2233"   # один номер в окне
assert core.call_phone("+7 777 111 2233 | +7 701 000 0000") is None                     # два — не угадываем
assert core.call_phone("Аня | Голосовой вызов") is None

own = "c:#users#u#appdata#local#dropletcalls#dropletcalls.exe"
assert core.call_app([own, "5319275a.whatsappdesktop_cv1g1gvanyjgm"], "dropletcalls.exe") == ("5319275a.whatsappdesktop_cv1g1gvanyjgm", "messenger")
assert core.call_app(["c:#program files#google#chrome#application#chrome.exe"], "dropletcalls.exe")[1] == "browser"
assert core.call_app([own], "dropletcalls.exe") is None

assert core.card("https://co.amocrm.ru/leads/detail/123?x=1") == ("leads", 123)
assert core.card("https://evil.ru/leads/detail/1") is None


def amo(contacts):
    return lambda path: (200, json.dumps({"_embedded": {"contacts": contacts}}))


def c(id, ph, leads=()):
    return {"id": id, "custom_fields_values": [{"field_code": "PHONE", "values": [{"value": ph}]}],
            "_embedded": {"leads": [{"id": l} for l in leads]}}


assert core.find(amo([c(1, "+7 (968) 609-71-19", [5, 9])]), "+7 968 609 7119") == ("leads", 9)
assert core.find(amo([c(1, "89686097119")]), "+7 968 609 7119") == ("contacts", 1)
assert core.find(amo([c(1, "+7 968 609 7119"), c(2, "8 968 609 71 19")]), "+7 968 609 7119") is None  # дубли — не пишем
assert core.find(amo([c(1, "+7 700 000 0000")]), "+7 968 609 7119") is None                         # нечёткий поиск
assert core.find(lambda p: (204, ""), "+7 968 609 7119") is None

r = 'Вот: {"город": "Алматы", "прайс": "—", "договорённости": ["созвон завтра", "счёт"]}'
assert core.note_text(r, 3, [("МОП", "Алло"), ("Клиент", "Да")]) == \
    "Итог звонка, 3 мин (Droplet)\n\nКЛЮЧЕВОЕ\nГород: Алматы\nДоговорённости: созвон завтра; счёт\n\nВЕСЬ ДИАЛОГ\nМОП: Алло\nКлиент: Да"
assert core.merge([(0, "Алло"), (5, "Сколько?")], [(2, "Да"), (3, "слушаю")]) == [("МОП", "Алло"), ("Клиент", "Да слушаю"), ("МОП", "Сколько?")]

s = {}
core.track_card(s, ("leads", 1)); core.track_card(s, None); core.track_card(s, ("leads", 1))
assert s == {"card": ("leads", 1)}
core.track_card(s, ("leads", 2))
assert s.get("changed") is True
print("ok")

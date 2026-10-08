import io
from datetime import date

import pytest
from openpyxl import Workbook

from companion.domain import Profile
from companion.importers import import_file, parse_ics, parse_text


def test_text_csv_json_and_xlsx():
    text = "Пн | 09:00-10:30 | Математика | 312 | В-78 | все"
    assert parse_text(text)[0].weekday == 0
    csv = "день;начало;конец;предмет;аудитория\nПн;09:00;10:30;Математика;312"
    assert parse_text(csv)[0].room == "312"
    assert parse_text('[{"date":"2026-10-07","start":"09:00","end":"10:30","title":"Встреча"}]')[
        0
    ].date == date(2026, 10, 7)
    book = Workbook()
    book.active.append(["дата", "начало", "конец", "предмет"])
    book.active.append(["07.10.2026", "09:00", "10:30", "Математика"])
    stream = io.BytesIO()
    book.save(stream)
    assert import_file("test.xlsx", stream.getvalue(), Profile(), date(2026, 10, 7))[0].title == "Математика"


def test_reject_ambiguous_rows_and_duplicates():
    with pytest.raises(ValueError, match="Строка 2"):
        parse_text("Пн | 09:00-10:30 | Математика\nТаблица не распознана")
    with pytest.raises(ValueError, match="повторяющиеся"):
        parse_text("Пн | 09:00-10:30 | Математика\nПн | 09:00-10:30 | Математика")


def test_ical_rrule_exdate_timezone_and_room_metadata():
    data = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:lesson-1\r\nDTSTART;TZID=Europe/Moscow:20261005T090000\r\nDTEND;TZID=Europe/Moscow:20261005T103000\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID=Europe/Moscow:20261012T090000\r\nSUMMARY:Math\r\nX-META-AUDITORIUM;NUMBER=312;CAMPUS=V-78:312\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:week-1\r\nDTSTART;VALUE=DATE:20261005\r\nDTEND;VALUE=DATE:20261012\r\nSUMMARY:Week\r\nEND:VEVENT\r\nEND:VCALENDAR"""
    rows = parse_ics(data, Profile(), date(2026, 10, 5), date(2026, 10, 20))
    assert [x.date for x in rows] == [date(2026, 10, 5), date(2026, 10, 19)]
    assert rows[0].start == "09:00"
    assert rows[0].room == "312" and rows[0].campus == "V-78"
    again = parse_ics(data, Profile(), date(2026, 10, 5), date(2026, 10, 20))
    assert rows[0].id == again[0].id


def test_empty_valid_calendar_clears_source_and_corrupt_file_is_rejected():
    empty = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR"
    assert parse_ics(empty, Profile(), date(2026, 10, 7), date(2026, 11, 7)) == []
    with pytest.raises(ValueError, match="Не удалось"):
        import_file("bad.xlsx", b"broken", Profile(), date(2026, 10, 7))

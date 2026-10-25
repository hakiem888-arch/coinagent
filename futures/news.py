"""Official CPI/FOMC calendar. Missing, stale, or unparseable sources fail closed."""
import calendar
import re
import threading
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import requests

BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
EASTERN = ZoneInfo("America/New_York")
MONTHS = {name.lower(): n for n in range(1, 13)
          for name in (calendar.month_name[n], calendar.month_abbr[n])}


def epoch(dt):
    return int(dt.timestamp() * 1000)


def cpi_events(text):
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        raise ValueError("Kalender CPI bukan iCalendar lengkap")
    text = re.sub(r"\r?\n[ \t]", "", text)
    events = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        if not re.search(r"^SUMMARY:.*Consumer Price Index", block, re.M | re.I):
            continue
        m = re.search(r"^DTSTART([^:]*):([^\r\n]+)", block, re.M)
        if not m:
            raise ValueError("CPI tidak memiliki waktu rilis")
        params, value = m.groups()
        dt = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            dt = dt.replace(tzinfo=timezone.utc)
        elif re.search(r"TZID=(US-Eastern|US/Eastern|America/New_York)(?:;|$)", params):
            dt = dt.replace(tzinfo=EASTERN)
        else:
            raise ValueError("Timezone CPI tidak dikenali; tidak diasumsikan UTC")
        events.append((epoch(dt), epoch(dt), "CPI AS"))
    if len(events) < 2:
        raise ValueError("Cakupan jadwal CPI tidak memadai")
    return sorted(set(events))


class FedParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.year, self.month, self.capture = None, None, None
        self.depth, self.parts, self.events, self.years = 0, [], [], {}

    def handle_starttag(self, tag, attrs):
        if self.capture:
            self.depth += 1
            return
        classes = dict(attrs).get("class", "").split()
        kind = "year" if tag == "h4" else ("month" if "fomc-meeting__month" in classes else
                                            ("date" if "fomc-meeting__date" in classes else None))
        if kind:
            self.capture, self.depth, self.parts = kind, 1, []

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if not self.capture:
            return
        self.depth -= 1
        if self.depth:
            return
        kind, text = self.capture, " ".join(" ".join(self.parts).split())
        self.capture = None
        if kind == "year":
            m = re.search(r"(20\d{2})\s+FOMC Meetings", text)
            self.year = int(m[1]) if m else None
        elif kind == "month":
            self.month = text
        elif self.year and self.month:
            months = [MONTHS.get(x.strip().lower()) for x in self.month.split("/")]
            days = [int(x) for x in re.findall(r"\d+", text)]
            if not months or not all(months) or not 1 <= len(days) <= 2:
                raise ValueError("Format tanggal FOMC berubah")
            start = datetime(self.year, months[0], days[0], tzinfo=EASTERN)
            end = datetime(self.year, months[-1], days[-1], tzinfo=EASTERN) + timedelta(days=1)
            if end <= start:
                raise ValueError("Rentang FOMC tidak valid")
            # Dates are official; block the whole meeting period conservatively
            # instead of pretending this page supplies exact announcement times.
            self.events.append((epoch(start), epoch(end), "FOMC: seluruh hari rapat (konservatif)"))
            self.years[self.year] = self.years.get(self.year, 0) + 1


def parse_calendars(bls, fed, now_ms):
    cpi = cpi_events(bls)
    parser = FedParser()
    parser.feed(fed)
    year = datetime.fromtimestamp(now_ms / 1000, timezone.utc).year
    if parser.years.get(year, 0) < 8:
        raise ValueError("Jadwal FOMC tahun berjalan tidak lengkap")
    start = max(cpi[0][0] - 35 * 86400000, epoch(datetime(year, 1, 1, tzinfo=EASTERN)))
    end = min(cpi[-1][0] + 86400000, epoch(datetime(year + 1, 1, 1, tzinfo=EASTERN)))
    if not start <= now_ms <= end:
        raise ValueError("Waktu sekarang di luar cakupan kalender resmi")
    return {"events": sorted(cpi + parser.events), "start": start, "end": end, "checked_ms": now_ms}


def clear_window(calendar_data, now_ms):
    if not calendar_data or not 0 <= now_ms - calendar_data["checked_ms"] <= 3600000:
        return {"ok": False, "reason": "Filter berita belum terverifikasi atau kedaluwarsa"}
    start, end = calendar_data["start"], calendar_data["end"]
    for begin, finish, name in calendar_data["events"]:
        if begin - 900000 <= now_ms <= finish + 900000:
            return {"ok": False, "reason": "WAIT · jendela berita " + name}
        if finish < now_ms:
            start = max(start, finish)
        elif begin > now_ms:
            end = min(end, begin)
    if start > now_ms - 900000 or end < now_ms + 900000:
        return {"ok": False, "reason": "Cakupan kalender belum mencakup ±15 menit"}
    return {"ok": True, "reason": "Jendela kalender CPI/FOMC lolos",
            "source": BLS_URL + " ; " + FED_URL, "checked_ms": calendar_data["checked_ms"],
            "from_ms": start, "until_ms": end}


class NewsCalendar:
    def __init__(self, session=None):
        self.session, self.data, self.retry_ms = session or requests.Session(), None, 0
        self.lock = threading.RLock()

    def window(self, now_ms):
        with self.lock:
            if self.data and 0 <= now_ms - self.data["checked_ms"] < 900000:
                return clear_window(self.data, now_ms)
            if now_ms < self.retry_ms:
                return {"ok": False, "reason": "Kalender resmi belum tersedia; jeda retry 60 detik"}
            try:
                texts = []
                for url in (BLS_URL, FED_URL):
                    response = self.session.get(url, timeout=(5, 10), allow_redirects=True)
                    response.raise_for_status()
                    if len(response.content) > 2_000_000:
                        raise ValueError("Kalender terlalu besar")
                    texts.append(response.text)
                self.data = parse_calendars(*texts, now_ms)
                return clear_window(self.data, now_ms)
            except (requests.RequestException, ValueError, KeyError, TypeError):
                self.retry_ms = now_ms + 60000
                return {"ok": False, "reason": "Kalender CPI/FOMC resmi gagal dimuat/divalidasi; sinyal baru WAIT"}

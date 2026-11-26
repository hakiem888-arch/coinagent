from datetime import datetime, timezone
import pytest
import requests
from futures.news import cpi_events, parse_calendars, clear_window, NewsCalendar


def ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()*1000)


def sources():
    bls="BEGIN:VCALENDAR\n"+"\n".join("BEGIN:VEVENT\nSUMMARY:Consumer Price Index\nDTSTART;TZID=US-Eastern:"+d+"\nEND:VEVENT"
        for d in ["20260212T083000","20260312T083000","20261014T083000","20261210T083000"])+"\nEND:VCALENDAR"
    fed='<h4><a>2026 FOMC Meetings</a></h4>'+''.join(
        f'<div class="fomc-meeting__month"><strong>{month}</strong></div><div class="fomc-meeting__date">{days}</div>'
        for month,days in [('January','27-28'),('March','17-18*'),('April','28-29'),('June','16-17'),('July','28-29'),('September','15-16'),('October','27-28'),('December','8-9')])
    return bls,fed


def test_eastern_dst_conversion_and_folded_ics():
    bls,_=sources()
    events=cpi_events(bls)
    assert events[0][0]==ms('2026-02-12T13:30:00')
    assert events[1][0]==ms('2026-03-12T12:30:00')
    assert cpi_events(bls.replace('Consumer Price Index','Consumer Price \n Index'))==events


def test_calendar_clear_and_fomc_whole_meeting_period_blocked():
    now=ms('2026-10-26T12:00:00')
    d=parse_calendars(*sources(),now)
    assert clear_window(d,now)["ok"]
    for moment in ['2026-10-27T10:00:00','2026-10-28T23:00:00']:
        check=ms(moment); d['checked_ms']=check
        assert not clear_window(d,check)["ok"]


def test_cpi_window_inclusive_and_stale_or_out_of_coverage_blocks():
    now=ms('2026-10-14T12:15:00')
    d=parse_calendars(*sources(),now)
    assert not clear_window(d,now)["ok"]
    assert not clear_window(d,now+3600001)["ok"]
    with pytest.raises(ValueError): parse_calendars(*sources(),ms('2027-02-01T00:00:00'))
    with pytest.raises(ValueError): parse_calendars(sources()[0],'<h4>empty</h4>',now)
    with pytest.raises(ValueError): cpi_events(sources()[0].replace(';TZID=US-Eastern',''))


def test_calendar_fetch_cached_and_errors_fail_closed():
    class Response:
        def __init__(self,text): self.text=text; self.content=text.encode()
        def raise_for_status(self): pass
    class Session:
        calls=0
        def get(self,*args,**kwargs):
            self.calls+=1
            if self.calls>2: raise requests.Timeout('fixture')
            return Response(sources()[self.calls-1])
    session=Session(); cal=NewsCalendar(session); now=ms('2026-10-26T12:00:00')
    assert cal.window(now)["ok"]
    assert cal.window(now+1000)["ok"] and session.calls==2
    assert not cal.window(now+900001)["ok"]
    assert not cal.window(now+900002)["ok"] and session.calls==3

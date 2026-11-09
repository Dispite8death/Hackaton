"""Dates reported in an archived news article; all comparisons use Magadan time."""

import re
from datetime import datetime, time, timedelta, timezone

from bs4 import BeautifulSoup

if __package__:
    from .news_cards import DATES, _parts
else:
    from news_cards import DATES, _parts


MAGADAN = timezone(timedelta(hours=11))
HAPPENED = re.compile(
    r"\b(?:состоялся|состоял[аио]сь|прош[её]л|прошл[аио]|провели|пров[её]л|провела|"
    r"провед[её]н[аыо]?|посетил[аи]?|побывал[аи]?|участвовал[аи]?|принял[аи]?\s+участие)\b", re.I)
EXCLUSIONS = re.compile(
    r"\b(?:предстоя\w*|состоится|пройд[её]т|планиру\w*|приглаша\w*|приказ\w*|постановлени\w*|"
    r"перенес[её]н\w*|отмен[её]н\w*)\b|\bне\s+(?:состоя|прош|провод|пров[её]|принял|посети|участв)", re.I)
BODY = "#description,.gw-current-news__body,[itemprop='articleBody'],article"
PUB = ("meta[property='article:published_time'],[itemprop='datePublished'],"
       "h1 + .gw-content-small-title,.gw-current-news__date,time[pubdate],time.published,.publication-date")
CLOCK = re.compile(r"^\s*[,;]?\s*(?:в\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")


def _value(node):
    return str(node.get("datetime") or node.get("content") or " ".join(node.stripped_strings)).strip()


def _points(raw):
    """Return closed time intervals. A missing year and multiple displayed days remain unresolved."""
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", raw):
        try:
            instant = datetime.fromisoformat(raw)
            instant = instant.astimezone(MAGADAN) if instant.tzinfo else instant.replace(tzinfo=MAGADAN)
            return [(instant, instant)]
        except ValueError:
            return [None]
    result = []
    for found in DATES.finditer(raw):
        clock = CLOCK.match(raw[found.end():])
        for year, month, day in _parts(found):
            try:
                if year is None:
                    result.append(None)
                    continue
                stamp = datetime(year,month,day,tzinfo=MAGADAN)
                if clock:
                    hour,minute,second = clock.groups()
                    stamp = stamp.replace(hour=int(hour),minute=int(minute),second=int(second or 0))
                    interval = (stamp,stamp)
                else:
                    interval = (stamp,datetime.combine(stamp.date(),time.max,MAGADAN))
                result.append(interval)
            except ValueError:
                result.append(None)
    return result


def dated_source(raw, field, **where):
    points = _points(raw)
    bounds = points[0] if len(points) == 1 else None
    return {"field":field,"raw":raw[:650],**where,
            "bounds":[d.isoformat() for d in bounds] if bounds else None}


def _resolved(refs):
    if not refs:
        return None,"date_missing"
    if any(ref["bounds"] is None for ref in refs):
        return None,"ambiguous_or_incomplete_date"
    windows = [(datetime.fromisoformat(ref["bounds"][0]),datetime.fromisoformat(ref["bounds"][1]))
               for ref in refs]
    lo, hi = max(w[0] for w in windows), min(w[1] for w in windows)
    return ((lo,hi),"agreed") if lo<=hi else (None,"conflicting_dates")


def _partial_last_possible(pub,event):
    """Prove a lower bound without assigning a year to an event reported as already over."""
    parts=set()
    for ref in event:
        if ref.get("field")!="reported_event_sentence" or ref.get("bounds"):
            return None
        match=DATES.fullmatch(ref["raw"].strip())
        if not match:
            return None
        points=_parts(match)
        if len(points)!=1 or points[0][0] is not None:
            return None
        parts.add(points[0][1:])
    if len(parts)!=1:
        return None
    month,day=parts.pop()
    try:
        last=datetime.combine(datetime(pub[1].year,month,day).date(),time.max,MAGADAN)
    except ValueError:
        return None
    # If this year's day is still ahead of the publication, do not infer a previous year.
    if last.date()>pub[1].date():
        return None
    return last


def latency(published, event):
    pub, why_pub = _resolved(published)
    happened, why_event = _resolved(event)
    windows = {"time_zone":"Asia/Magadan","publication_resolution":why_pub,"event_resolution":why_event}
    if pub and not happened:
        last=_partial_last_possible(pub,event)
        if last and pub[0]-last>timedelta(hours=24):
            return "over_24h",dict(windows,reason="over_24h_for_every_possible_past_year",
                                   latest_possible_event_bound=last.isoformat(),
                                   publication_window=[x.isoformat() for x in pub])
    if not pub or not happened:
        return "uncertain",windows
    for label,w in (("publication_window",pub),("event_window",happened)):
        windows[label] = [v.isoformat() for v in w]
    lower, upper = pub[0]-happened[1], pub[1]-happened[0]
    if upper < timedelta():
        return "published_before_event",windows
    if lower > timedelta(hours=24):
        return "over_24h",windows
    if upper <= timedelta(hours=24):
        # The article explicitly reports that this event already happened.
        return "within_24h",windows
    return "uncertain",dict(windows,reason="clock_time_needed")


def _sentences(text):
    # Preserve day.month.year and the Russian year abbreviation inside a date.
    text = re.sub(r"(\d{4})\s+г\.(?=\s)", r"\1 г", text)
    return re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z\d🗓📆📅])", text)


def article_sources(html, page_id):
    soup = BeautifulSoup(html,"html.parser")
    pub = [dated_source(value,"article_published",page_id=page_id,selector=selector)
           for selector in PUB.split(",") for node in soup.select(selector)
           if not node.find_parent(["aside","nav","footer","template"])
           if (value:=_value(node))]
    main = soup.select_one("main,[role=main]") or soup.body or soup
    h1 = main.select_one("h1")
    title = h1.get_text(" ",strip=True) if h1 else ""
    body = main.select_one(BODY) or main
    for node in body.select("nav,aside,footer,header,script,style,template,[hidden],.breadcrumb,.related-news"):
        node.decompose()
    links = [a.get("href","") for a in body.select("a[href]")]
    texts = []
    for node in body.select("p,li"):
        if not node.find(["p","li"]):
            texts.append(" ".join(node.stripped_strings))
    if not texts and body is not main:
        texts = [" ".join(body.stripped_strings)]
    event, reports = [], []
    for text in texts:
        for sentence in _sentences(text):
            verbs = list(HAPPENED.finditer(sentence))
            if not verbs or EXCLUSIONS.search(sentence):
                continue
            reports.append(sentence[:600])
            matches = list(DATES.finditer(sentence))
            for match in matches:
                before = sentence[:match.start()]
                if re.search(r"\b(?:к|ко|до|после|котор\w*|приуроченн\w*|посвященн\w*)\s*$",before,re.I):
                    continue
                if min(min(abs(v.start()-match.end()),abs(match.start()-v.end())) for v in verbs)>125:
                    continue
                # A list of dates, or a date range, cannot anchor a single occurrence.
                if len(matches)>1 or re.search(r"\bс\s*$",before,re.I) and re.match(r"\s+по\b",sentence[match.end():],re.I):
                    event.append({"field":"reported_event_sentence","raw":sentence[:650],"page_id":page_id,"bounds":None})
                    break
                if any(re.match(r"\s+(?:[а-яё-]+\s+){0,2}(?:неделя|месячник)\b",sentence[v.end():],re.I) for v in verbs):
                    event.append({"field":"reported_period","raw":sentence[:650],"page_id":page_id,"bounds":None})
                    break
                raw = match.group()
                clock = CLOCK.match(sentence[match.end():])
                if clock:
                    raw += clock.group()
                event.append(dated_source(raw,"reported_event_sentence",page_id=page_id,quote=sentence[:650]))
    if reports:
        for node in body.select("[itemprop='startDate'],[data-event-date]"):
            value = node.get("data-event-date") or _value(node)
            if value:
                event.append(dated_source(value,"structured_event_date",page_id=page_id))
    return {"title":title,"published":pub,"event":event,"reports":reports,"links":links}

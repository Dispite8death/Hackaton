"""Read the institution's own public contact fields from a kindergarten homepage."""

import re
from collections import Counter
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment, Tag

if __package__:
    from .staff_extract import _selector
else:
    from staff_extract import _selector


FIELDS = ("address", "phone", "email", "hours")
ORG = r"(?:\s+(?:детского сада|учреждения|организации|доу|м[абк]доу))?"
LABELS = {
    "address": re.compile(r"(?:адрес(?: места нахождения)?|(?:почтовый|юридический|фактический) адрес|"
                          r"место нахождения(?: образовательной организации)?)" + ORG, re.I),
    "phone": re.compile(r"(?:контактн\w*\s+)?(?:телефон(?:ы)?|тел\.?)" + ORG, re.I),
    "email": re.compile(r"(?:адрес\s+)?(?:электронной почты|электронная почта|эл\.?\s*почта|e-?mail)" + ORG, re.I),
    "hours": re.compile(r"(?:(?:режим|график|часы)\s+работы|режим и график работы)" + ORG, re.I),
}
AREA = re.compile(r"(?:^|[\s_-])(?:contacts?|kontakty|contact-info|organization-contacts)(?:$|[\s_-])", re.I)
CONTACT_TITLE = re.compile(r"(?:контакты(?: учреждения| детского сада)?|контактная информация|"
                           r"контактные данные|как (?:нас найти|добраться)|обратная связь)", re.I)
EXCLUDED = re.compile(r"(?:^|[\s_-])(?:news|novosti|person|staff|employee|director|profile|event|"
                      r"author|hotline|emergency|support|developer|feedback-widget|document-item|hidden-content-card)"
                      r"(?:$|[\s_-])", re.I)
OTHER_OWNER = re.compile(r"заведующ|директор|воспитател[ья]|специалист|психолог|логопед|сотрудник|"
                         r"час[ыа]\s+при[её]ма|(?:личн\w*\s+)?при[её]м[а]?\s+(?:граждан|родителей)|"
                         r"горяч\w*\s+лини|техническ\w* поддержк|техподдержк|разработчик|автор\b|"
                         r"служб[аы] (?:поддержки|спасения)|экстренн\w* служб|еддс|мчс|министерств|"
                         r"департамент|учредител|полици|антитеррор", re.I)
PHONE = re.compile(r"(?<!\w)(?:\+?\d|\(\d)[\d\s().\-–]{3,27}\d(?!\w)")
EMAIL = re.compile(r"[\w.+%-]+@[\w.-]+\.[A-Za-zА-Яа-яЁё]{2,}", re.I)
ADDRESS = re.compile(r"\b(?:ул\.|улиц\w*|пр\.|пр-?к?т\.?|проспект|пер\.|переулок|дом\b|д\.\s*\d|"
                     r"г\.\s*[а-яё]|город\b|пос[её]лок|пгт\.?|село\b|шоссе|набережн\w*|\d{6}\b)", re.I)
SCHEDULE = re.compile(r"\b(?:пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|сред[ауы]|четверг|пятниц\w*|"
                      r"суббот\w*|воскресень\w*|выходн\w*|празднич\w*|пятиднев\w*|ежедневно|"
                      r"круглосуточно|будн\w*|перерыв|\d{1,2}\s*час\w*)\b|\d{1,2}[:.]\d{2}", re.I)
HOURS_OWNER = re.compile(r"(?:(?:режим|график|часы)\s+(?:работы\s+)?(?:заведующ|директор|специалист|"
                         r"воспитател|психолог|логопед|медицинск)|(?:часы|график|время)\s+(?:личного\s+)?при[её]ма|"
                         r"личный\s+при[её]м|при[её]м\s+граждан)", re.I)
ORG_HOURS = re.compile(r"(?:режим|график|часы) работы\s+(?:детского сада|учреждения|доу|м[абк]доу)", re.I)
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6", "legend", "caption")


def _text(tag):
    if isinstance(tag, Tag):
        return " ".join(tag.get_text(" ", strip=True).split())
    return " ".join(str(tag or "").split())


def _key(field, value):
    if field == "phone":
        digits = re.sub(r"\D", "", value)
        return "7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits
    value = value.casefold().replace("ё", "е")
    if field == "hours":
        value = re.sub(r"\b(\d{1,2})[.:](\d{2})", lambda m: f"{int(m[1]):02}:{m[2]}", value)
    return re.sub(r"[\s.,:;–—-]+", " ", value).strip() if field != "email" else value


def _scope(node):
    """Find where the contact was published, stopping personal and outside-service subtrees."""
    location, contact_area, child = "main", False, None
    for tag in (node, *node.parents):
        marks = " ".join([tag.get("id", ""), *tag.get("class", [])])
        if (tag.name in {"nav", "aside", "template", "script", "style", "form"} or tag.has_attr("hidden")
                or tag.get("role") == "navigation" or EXCLUDED.search(marks)
                or tag.get("itemtype", "").rstrip("/").endswith("/Person")):
            return None, False
        if child is not None:
            heading = child.find_previous_sibling(HEADINGS)
            title = _text(heading)
            if OTHER_OWNER.search(title) or re.search(r"новости|события|мероприятия", title, re.I):
                return None, False
            contact_area |= bool(CONTACT_TITLE.fullmatch(title))
        contact_area |= bool(AREA.search(marks) or tag.get("itemtype", "").rstrip("/").endswith(
                             ("/Organization", "/EducationalOrganization", "/Preschool", "/School")))
        if "gw-gradient-block__contact-item-block" in tag.get("class", []):
            location, contact_area = "under_header", True
        elif tag.name in {"header", "footer"}:
            return (location if location == "under_header" else tag.name), True
        child = tag
    return location, contact_area


def _schedules(raw):
    selected, other_groups, personal = [], [], None
    for line in raw.splitlines():
        line = _text(line)
        if ORG_HOURS.match(line):
            personal = None
        other = HOURS_OWNER.search(line)
        if other:
            before = line[:other.start()].strip(" ;,.")
            if personal is None and SCHEDULE.search(before):
                selected.append(before)
            personal = [line[other.start():]]
            other_groups.append(personal)
        elif SCHEDULE.search(line) and not re.match(r"адрес|место нахождения", line, re.I):
            (selected if personal is None else personal).append(line)
    return (" ".join(dict.fromkeys(selected)),
            [(lines[0], " ".join(dict.fromkeys(lines))) for lines in other_groups if SCHEDULE.search(" ".join(lines))])


def _values(field, raw):
    flat = _text(raw)
    if OTHER_OWNER.search(flat):
        return []
    if field == "email":
        return EMAIL.findall(flat)
    if field == "phone":
        return [v for v in PHONE.findall(flat) if 5 <= len(_key(field, v)) <= 15]
    flat = re.sub(r"^(?:адрес|место нахождения образовательной организации)\s*:\s*", "", flat, flags=re.I)
    return [flat] if len(flat) <= 550 and ADDRESS.search(flat) and re.search(r"\d", flat) else []


def _fields(label):
    return next((field for field, pattern in LABELS.items() if pattern.fullmatch(label.strip(" .:"))), None)


def _paired_value(label_node):
    # An inline b/span often has a plain-text sibling. A dt/th usually has a dd/td sibling.
    for sibling in label_node.next_siblings:
        if isinstance(sibling, Comment) or not _text(sibling).strip(" :–—-"):
            continue
        if isinstance(sibling, Tag):
            if sibling.name in (*HEADINGS, "dt", "th") or _fields(_text(sibling)):
                return None, ""
            return sibling, sibling.get_text("\n", strip=True)
        return label_node.parent, str(sibling)
    return None, ""


def _other_contact(node):
    # Utility services and personal contacts also appear inside otherwise legitimate contact sections.
    for part in (node, *list(node.parents)[:3]):
        if part.name in {"main", "body", "section", "header", "footer"}:
            break
        previous = part.find_previous_sibling()
        if previous and not previous.find(["a", "p", "section", "article"]):
            label = _text(previous)
            if len(label) <= 100 and OTHER_OWNER.search(label):
                return True
        if part.name in {"li", "p", "address"} and OTHER_OWNER.search(_text(part)):
            return True
    return False


def extract_garden_contacts(html, url):
    """Return address, phone, email and hours with public DOM evidence. No network or database IO."""
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")
    ids = Counter(node["id"] for node in soup.find_all(id=True))
    observations = {field: {} for field in FIELDS}
    other_schedules = {}
    template_rows = {id(row) for row in soup.select(".gw-gradient-block__contact-item-block")}

    def capture(field, node, raw, label, method, *, trusted=False):
        location, area = _scope(node)
        if not location or not (trusted or area) or (field != "hours" and _other_contact(node)):
            return
        if field == "hours":
            institution, local_schedules = _schedules(raw)
            values = [institution] if institution else []
            for heading, text in local_schedules:
                selector = _selector(node, ids)
                source = {"selector": selector, "location": location, "context": text[:460], "method": method,
                          "page_url": url}
                entry = other_schedules.setdefault(_key(field, text), {"label": heading, "value": text, **source,
                                                                        "sources": []})
                if source not in entry["sources"]:
                    entry["sources"].append(source)
        else:
            values = _values(field, raw)
        for value in values:
            selector = _selector(node, ids)
            source = {"selector": selector, "location": location, "context": (label + " · " + value)[:460],
                      "method": method, "page_url": url}
            group = observations[field].setdefault(_key(field, value), {"value": value, **source, "sources": []})
            if source not in group["sources"]:
                group["sources"].append(source)

    for caption in soup.select(".gw-gradient-block__contact-item-block-caption"):
        field = _fields(_text(caption))
        node = caption.find_next_sibling(class_="gw-gradient-block__contact-item-block-text")
        if field and node is not None:
            capture(field, node, node.get_text("\n", strip=True), _text(caption), "template", trusted=True)
            if field in {"email", "phone"}:
                scheme = "mailto:" if field == "email" else "tel:"
                for link in node.select(f'a[href^="{scheme}" i]'):
                    raw = unquote(link["href"][len(scheme):].partition("?")[0])
                    capture(field, link, raw, _text(caption), "link_target", trusted=True)

    for txt in soup.find_all(string=True):
        if isinstance(txt, Comment) or not txt.strip() or len(txt) > 1200:
            continue
        node = txt.parent
        label, colon, inline = _text(txt).partition(":")
        field = _fields(label)
        if not field or any(id(p) in template_rows for p in (node, *node.parents)):
            continue
        # An explicit field naming the kindergarten can stand alone outside a contact section.
        explicit = bool(re.search(r"детского сада|организации|учреждения|доу", label, re.I))
        if colon and inline.strip():
            capture(field, node, inline, label, "label", trusted=explicit)
        else:
            value_node, raw = _paired_value(node)
            if value_node is not None:
                capture(field, value_node, raw, label, "label", trusted=explicit)

    for node in soup.select('a[href^="mailto:" i], a[href^="tel:" i], address, [itemprop]'):
        if any(id(p) in template_rows for p in (node, *node.parents)):
            continue
        props = node.get("itemprop", "").split()
        field = next((v for k, v in {"address":"address", "telephone":"phone", "email":"email",
                                     "openingHours":"hours"}.items() if k in props), None)
        href = node.get("href", "")
        if not field:
            field = "address" if node.name == "address" else {"mailto":"email", "tel":"phone"}.get(href.partition(":")[0].lower())
        if not field:
            continue
        local = node.parent if node.parent and node.parent.name in {"p", "li", "address"} else node
        if OTHER_OWNER.search(_text(local)) or OTHER_OWNER.search(node.get("aria-label", "")):
            continue
        raw = node.get("content") or node.get("datetime") or node.get_text("\n", strip=True)
        if field in {"phone", "email"} and not _values(field, raw) and href.lower().startswith(("tel:", "mailto:")):
            raw = unquote(href.partition(":")[2].partition("?")[0])
        capture(field, node, raw, _text(local)[:130], "markup")

    result = {}
    for field, groups in observations.items():
        values = list(groups.values())
        primary = [v for v in values if v["location"] == "under_header"]
        # Multiple addresses or numbers can legitimately belong to one institution. Keep the evidence for review.
        status = "not_found" if not values else "ambiguous" if len(values) > 1 else "found"
        result[field] = {"status": status, "values": primary + [v for v in values if v not in primary]}
    result["hours"]["other_schedules"] = list(other_schedules.values())
    return result

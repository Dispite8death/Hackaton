"""Evidence for public contacts by DOM location, retaining combined opening/reception blocks."""

import re
from collections import Counter
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment, Tag

if __package__:
    from . import garden_contacts as garden
    from .home_content import _selector
    from .navigation import resolve_url, words
    from .http_page import same_site
else:
    import garden_contacts as garden
    from home_content import _selector
    from navigation import resolve_url, words
    from http_page import same_site


CONTACT_FIELDS = ("address", "phone", "email", "hours", "reception")
LABELS = {
    "address": re.compile(r"(?:(?:полный|юридический|фактический|почтовый) )?адрес(?: (?:школы|сада|учреждения|организации|места нахождения))?|место нахождения", re.I),
    "phone": re.compile(r"(?:контактн\w* )?(?:телефон[ы]?|тел\.?)\s*(?:\((?:при[её]мная|при[её]мной)\)|школы|учреждения)?", re.I),
    "email": re.compile(r"(?:адрес )?(?:электронн\w+ почт\w+|эл\.?(?:ектр\.?)? почта|e-?mail)", re.I),
}
OPENING = re.compile(r"(?:график|время|часы|режим)(?: и график)?\s+работы|открыт[аы]?\s+(?:с|для)", re.I)
RECEPTION = re.compile(r"(?:часы|время|дни|график|режим)\s+(?:личн\w*\s+)?при[её]ма|(?:личный\s+)?при[её]м\s+(?:граждан|родител\w*)|(?:часы|график|время|режим) работы при[её]мной", re.I)
PERSONAL = re.compile(r"работы\s+(?:специалист|заведующ|психолог|директор|медицинск|воспитател)", re.I)
FOREIGN = re.compile(r"горяч\w*\s+лини|еддс|мчс|министерств|департамент|техподдержк|экстренн\w*\s+служб", re.I)
BLOCKED = re.compile(r"(?:^|[\s_-])(?:news|events?|person|teachers?|staff|social|banner|hotline|breadcrumb|developer)(?:$|[\s_-])",re.I)
HIDDEN = {"script","style","template","noscript","form"}
CLASS_FIELDS = ".tpl-text-header6,.gw-content-small-title,.gw-gradient-block__contact-item-block-caption,.gw-footer-contact__title,dt,th"


def clean(node):
    return " ".join((node.get_text(" ",strip=True) if isinstance(node,Tag) else str(node or "")).split())


def _classes(node):
    return " ".join([node.get("id",""), *node.get("class",())])


def location(node, section):
    for el in (node,*node.parents):
        classes = _classes(el)
        if el.name in HIDDEN or el.has_attr("hidden") or el.get("aria-hidden")=="true" or BLOCKED.search(classes):
            return None
        if "gw-gradient-block__contact-item-block" in classes or re.search(r"(?:^|\s)(?:intro-contacts|hero-contacts)(?:$|\s)",classes):
            return "under_header"
        if el.name=="header" or el.get("role")=="banner" or "gw-header" in classes:
            return "header"
        if el.name=="footer" or "gw-footer" in classes:
            return "footer"
        if el.name in {"main","body"}:
            break
    return "contacts_page" if section=="contacts" else "home_body"


def _fields(label):
    label = label.strip(" .:—–-")
    if not label or len(label)>105 or FOREIGN.search(label):
        return ()
    named = [field for field, pattern in LABELS.items() if pattern.fullmatch(label)]
    if re.search(r"\d[.:]\d|(?:пн|пятниц\w*|понедельник)\b",label,re.I):
        return named
    if OPENING.search(label) and not PERSONAL.search(label) and not re.search(r"работы при[её]мной",label,re.I):
        named.append("hours")
    if RECEPTION.search(label):
        named.append("reception")
    return named


def _paired(label):
    """A school CMS title and its value are independently wrapped sibling widgets."""
    wrapper = label.find_parent(class_="tpl-component-gw-base-title")
    if wrapper and wrapper.parent:
        value = wrapper.parent.find_next_sibling()
        if value and not value.select_one(".tpl-component-gw-base-title"):
            return value, value.get_text("\n",strip=True)
        return label,""
    if "gw-content-small-title" in label.get("class",()):
        siblings = label.find_next_siblings()
        if siblings:
            lines = []
            for sibling in siblings:
                if sibling.select_one(CLASS_FIELDS) or _fields(clean(sibling)):
                    break
                lines.append(sibling.get_text("\n",strip=True))
            return label.parent, "\n".join(lines)
    return garden._paired_value(label)


def _schedule(value):
    return bool(garden.SCHEDULE.search(value) or re.search(r"предварительн\w*\s+запис|по\s+записи",value,re.I))


def extract_contacts(soup, url, *, section):
    """Each entry is a single observation. Multiple numbers and different places remain traceable."""
    if not isinstance(soup,BeautifulSoup):
        soup=BeautifulSoup(soup,"html.parser")
    ids=Counter(el["id"] for el in soup.find_all(id=True))
    entries, seen = [], set()

    def add(field, value, node, raw, label, method):
        scope=location(node,section)
        if not value or not scope:
            return
        key=(field,garden._key(field if field in garden.FIELDS else "hours",value),scope,_selector(node,ids))
        if key in seen:
            return
        seen.add(key)
        entries.append({"field":field,"value":clean(value),"location":scope,"selector":key[3],
                        "page_url":url,"label":label,"block_text":raw,"method":method})

    def record(label,node,raw,fields):
        flat=clean(raw)
        if not flat or len(flat)>3500 or FOREIGN.search(label):
            return
        for field in fields:
            if field in ("hours","reception"):
                main, others = garden._schedules(raw)
                reception_parts = [text for _,text in others if RECEPTION.search(text)]
                reception_match=RECEPTION.search(raw)
                if not reception_parts and reception_match:
                    before,after=raw[:reception_match.start()],raw[reception_match.start():]
                    main=garden._schedules(before)[0]
                    reception_parts=[after]
                if field=="reception":
                    part=" ".join(reception_parts) if "hours" in fields and reception_parts else flat
                    if _schedule(part):
                        add(field,part,node,flat,label,"labelled_schedule")
                else:
                    if main and _schedule(main):
                        add(field,main,node,flat,label,"labelled_schedule")
                    for part in reception_parts:
                        if _schedule(part):
                            add("reception",part,node,flat,label,"combined_block")
                continue
            for line in raw.splitlines():
                # The school's reception phone is a public institution contact, even with a staff qualifier.
                if FOREIGN.search(line):
                    continue
                if field=="phone":
                    phones=[v for v in garden.PHONE.findall(line) if 5 <= len(garden._key("phone",v)) <= 15]
                    for value in phones:
                        add(field,value,node,flat,label,"labelled_value")
                elif field=="email":
                    for value in garden.EMAIL.findall(line):
                        add(field,value,node,flat,label,"labelled_value")
            if field=="address" and garden.ADDRESS.search(flat) and re.search(r"\d",flat):
                add(field,flat,node,flat,label,"labelled_value")

    candidates = list(soup.select(CLASS_FIELDS))
    # Non-template headings and simple inline labels in the named contact area or the page header.
    for tag in soup.select("p,label,strong,b,h2,h3,h4,h5,h6,span,div"):
        if tag.string and isinstance(tag.string,str) and not isinstance(tag.string,Comment):
            candidates.append(tag)
    visited=set()
    for tag in candidates:
        if id(tag) in visited:
            continue
        visited.add(id(tag))
        block=tag.find_parent(class_="gw-gradient-block__contact-item-block")
        if block and "gw-gradient-block__contact-item-block-caption" not in tag.get("class",()):
            continue
        text=clean(tag)
        if len(text)>500:
            continue
        label, sep, inline=text.partition(":")
        fields=_fields(label)
        if not fields or not location(tag,section) or tag.find_parent(["nav","a"]):
            continue
        # A heading below a staff/news card is not an institution contact. Bound generic labels to appropriate areas.
        if (location(tag,section)=="home_body" and not tag.find_parent(class_=garden.AREA)
                and not tag.find_parent(class_="tpl-component-gw-base-title")):
            continue
        if sep and inline.strip():
            record(label,tag,inline.strip(),fields)
        elif len(label)<=110:
            value_node,raw=_paired(tag)
            if value_node is not None:
                record(label,value_node,raw,fields)

    for tag in soup.select("a[href^='tel:' i],a[href^='mailto:' i],address,[itemprop~='streetAddress'],[itemprop~='openingHours']"):
        scope=location(tag,section)
        if not scope or (scope=="home_body" and not tag.find_parent(class_=garden.AREA)):
            continue
        if garden._other_contact(tag) or FOREIGN.search(clean(tag.parent)[:300]):
            continue
        href=unquote(tag.get("href","")).split("?",1)[0]
        field="email" if href.lower().startswith("mailto:") else "phone" if href.lower().startswith("tel:") else (
            "hours" if "openingHours" in tag.get("itemprop","") else "address")
        raw=tag.get("content") or clean(tag) or href.partition(":")[2]
        if field in ("phone","email") and not garden._values(field,raw):
            raw=href.partition(":")[2]
        record(tag.get("aria-label") or field,tag,raw,[field])

    if section=="contacts":
        for marker in soup.select("main [id^='kontaktnye-telefony'],main [id='telefon-shkoly']"):
            container=marker.find_parent(class_="tpl-component-gw-base-text")
            if container and location(container,section)=="contacts_page":
                record(marker["id"],container,container.get_text("\n",strip=True),["phone"])
    for tag in soup.select("header .gw-content-small-title,header .gw-header__subtitle"):
        raw=clean(tag)
        if garden.ADDRESS.search(raw) and re.search(r"\d",raw) and re.search(r"ул\.|улиц|\bд\.|просп|пр-[кт]|пер\.|шоссе",raw,re.I):
            add("address",raw,tag,raw,"Подпись учреждения","header_address")
    # Alternate layouts can publish a bare number, email or street address without an anchor or a field caption.
    for tag in soup.select("header p,header span,header div,[role=banner] p,[role=banner] span,.hero-contacts p,.intro-contacts p"):
        if tag.find(True) or location(tag,section) not in {"header","under_header"} or tag.find_parent(["nav","a"]):
            continue
        raw=clean(tag)
        if not raw or len(raw)>220 or garden.OTHER_OWNER.search(raw):
            continue
        if garden.EMAIL.fullmatch(raw):
            add("email",raw,tag,raw,"","plain_header_value")
        elif garden.PHONE.fullmatch(raw) and len(garden._key("phone",raw)) in (10,11):
            add("phone",raw,tag,raw,"","plain_header_value")
        elif re.search(r"(?:\bул\.|\bулица\b|\bпер\.|\bпроспект\b|\bшоссе\b)",raw,re.I) and re.search(r"\d",raw):
            add("address",raw,tag,raw,"","plain_header_value")
    return entries


def section_links(soup, url, site):
    ids = Counter(el["id"] for el in soup.find_all(id=True))
    result=[]
    for a in soup.select("a[href]"):
        label=clean(a) or clean(a.get("aria-label")) or clean(a.get("title"))
        key=words(label)
        if key in {"контакты","контактная информация","контакты школы","контакты детского сада","контактные данные"}:
            section="contacts"
        elif key in {"расписание","расписание занятий","расписание уроков","учебное расписание"}:
            section="schedule"
        else:
            continue
        href=a["href"].strip()
        if not href or href=="#":
            continue
        target=resolve_url(url,href)
        if not target:
            continue
        scope=location(a,"home")
        if not scope:
            continue
        result.append({"section":section,"url":target,"href":href,"label":label,"location":scope,
                       "internal":same_site(target,site),"selector":_selector(a,ids)})
    return result

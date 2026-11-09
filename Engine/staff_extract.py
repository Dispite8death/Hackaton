"""Extract staff-card candidates and snapshot-local evidence for browser review."""

from collections import Counter
from dataclasses import dataclass
import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Tag


@dataclass(frozen=True)
class ElementEvidence:
    field: str
    selector: str
    text: str


@dataclass(frozen=True)
class StaffCard:
    name: str | None
    role: str | None
    is_head: bool
    photo_url: str | None
    photo_state: str
    education: str | None
    email: str | None
    phone: str | None
    profile_url: str | None
    method: str
    elements: tuple[ElementEvidence, ...]


# Each selector is a boundary for ONE employee, never a list of employees.
CARDS = (".tpl-component-gw-staff .object-item, .gw-person-card, .gw-person-detail, "
         ".gw-personal-card, .gw-person-info-card, .gw-employee-info")
NAME = (".contacts-object-info.tpl-text-header3 a, .gw-person-card__name, .gw-person-detail__name, "
        ".gw-personal-card__name, .gw-person-info-card__name, .gw-employee-info__title")
ROLE = (".contacts-object-info-subname, .gw-person-card__job, .gw-person-detail__job, "
        ".gw-personal-card__name-subscription, .gw-person-info-card__position, .gw-employee-info__position")
PHOTO = (".object-image img, .item-image img, .gw-person-card__image img, .gw-person-detail__img, "
         ".gw-personal-card__img, .gw-person-info-card__img, .gw-employee-info__img")
ACTING = r"(?:(?:и\s*\.?\s*о\.?|врио|временно\s+исполняющ\w*\s+обязанности|исполняющ\w*\s+обязанности)\s*)?"
LEADER = re.compile(
    ACTING + r"(?:директор(?:а)?|заведующ(?:ий|ая|его|ей)|руководитель)\s*"
    r"(?:(?:школы|школой|лицея|гимназии|детского\s+сада|детским\s+садом|учреждения|образовательной\s+организации)\b"
    r"|(?:м[абк](?:д)?оу|доу|дошкольн\w*)\b[^,;]{0,90})?\s*(?:[,;].*)?$", re.I)
STAFF_ROLE = re.compile(
    r"^(?:заместитель|зам\.|советник|учитель|преподаватель|старший\s+воспитатель|воспитатель|"
    r"педагог|логопед|психолог|методист|музыкальный\s+руководитель|инструктор|заведующ\w*)\b"
    r"[\w\s.,()/-]{0,95}$", re.I)
ROLE_WORD = re.compile(r"директор|заведующ|руководител|учител|воспитател|педагог|методист|логопед|инструктор|психолог|преподавател", re.I)
WORD = r"[А-ЯЁ][а-яёА-ЯЁ]+(?:[-’'][А-ЯЁа-яё]+)*"
FULL_NAME = re.compile(rf"(?:{WORD}(?:\s+{WORD}){{1,3}}|{WORD}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)$")
NOT_NAME = re.compile(r"\b(?:Фамилия|Имя|Отчество|Директор|Заместитель|Педагог|Учитель|Школа|"
                      r"Руководитель|Телефон|Почта|Воспитатель|Высшее|Образование|Сведения|"
                      r"Детский|Сотрудник|Приём|График|Контактный|Электронная)\b", re.I)
IGNORED = re.compile(r"(?:^|[ _-])(?:news|event|program|course|lesson|navigation|breadcrumb|card-footer|"
                     r"gw-document-item(?:-tooltip)?|novosti|meropriyatiya|novost)(?:$|[ _-])", re.I)
SIGNATURE_LABEL = re.compile(r"\b(?:дата и время подписания документа|(?:подписавшего|подписавшей) документ)\b", re.I)
UNIT_LABEL = re.compile(r"\b(?:совет\w*|собрани\w*|комитет\w*|коллегиальн\w*|отделени\w*|"
                        r"филиал\w*|представительств\w*|подразделени\w*|объединени\w*|совещани\w*)\b", re.I)
INSTITUTION_LABEL = re.compile(r"\b(?:единоличн\w*\s+исполнительн\w*\s+орган|"
                               r"административно\s*[-–]?\s*управленческ\w*\s+персонал|"
                               r"[а-яё]{1,7}оу|школа|сош|лицей|гимназия|детский\s+сад)\b", re.I)
UNIT_COLUMN = re.compile(r"(?:наименование|название)\s+(?:(?:структурного\s+)?подразделения|органа управления)", re.I)
PLACEHOLDER = re.compile(r"placeholder|no[-_ ]?(?:photo|avatar|image)|заглушк|фото\s+отсутствует", re.I)
EDUCATION = re.compile(r"(?:(?:сведения\s+об?\s+)?уров(?:ень|не|ни)\b.{0,40}образования\b.*|(?:профессиональное\s+)?образование)", re.I)
MAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}", re.I)
PHONE = re.compile(r"\+?\d[\d()\s–-]{3,26}\d")


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def _name(value):
    clean = " ".join(value.split()).rstrip(",;")
    if clean.endswith(".") and not re.search(r"\b[А-ЯЁ]\.$", clean):
        clean = clean[:-1]
    return clean if FULL_NAME.fullmatch(clean) and not NOT_NAME.search(clean) else None


def _role(value):
    value = " ".join(value.split()).strip(" ,;:")
    return value if value and (LEADER.fullmatch(value) or STAFF_ROLE.fullmatch(value)) else None


def _url(page_url, value):
    try:
        url = urljoin(page_url, value)
        parts = urlsplit(url)
        return url if value and parts.scheme in ("http", "https") and parts.hostname and not parts.username else None
    except ValueError:
        return None


def _selector(tag, ids):
    """CSS locator in this particular snapshot. Stable id first, structural path otherwise."""
    path = []
    while isinstance(tag, Tag) and tag.name != "[document]":
        identifier = tag.get("id", "")
        if ids[identifier] == 1 and re.fullmatch(r"[a-zA-Z_][\w-]*", identifier):
            path.append("#" + identifier)
            break
        index = 1 + sum(1 for _ in tag.find_previous_siblings(tag.name))
        path.append(f"{tag.name}:nth-of-type({index})")
        tag = tag.parent
    # A browser inserts tbody around direct children of table on HTML parsing.
    return re.sub(r"(table:nth-of-type\(\d+\)|#[\w-]+) > tr:", r"\1 tr:", " > ".join(reversed(path)))


def _ignored(node, known):
    for tag in (node, *node.parents):
        if id(tag) in known or tag.name in {"nav", "footer", "header", "aside", "script", "style", "template", "form", "option"}:
            return True
        mark = " ".join(tag.get("class", []))
        if IGNORED.search(mark) or tag.get("role") == "navigation":
            return True
        if tag.name == "article" and not re.search("person|staff|employee|teacher|сотрудник", mark, re.I):
            return True
    return False


def _name_nodes(container):
    result = {}
    for node in container.select('[itemprop="name"], [itemprop="fio"]'):
        name = _name(_text(node))
        if name:
            result.setdefault(name, node)
    for text in container.find_all(string=True):
        if isinstance(text, Comment) or text.parent.name in {"script", "style", "template"}:
            continue
        found = _name(str(text))
        if found:
            result.setdefault(found, text.parent)
            if len(result) > 1:
                break
    return result


def _governance_scope(node):
    """Read the caption of this governance record, never the heading of a whole directory."""
    for box in (node, *node.parents):
        classes = box.get("class", [])
        if "subdivision-text" in classes:
            heading = box.find(class_="subdivision-name", recursive=False)
        elif "gw-filled-section_color_highlighted" in classes and box.find_parent("section", id=re.compile(
                r"^(?:kollegialnye|strukturnye|strukturno|organy-upravleniya)")):
            heading = box.find(["h4", "h5", "h6"], recursive=False)
        elif box.name == "tr":
            table = box.find_parent("table")
            header = table.find("tr") if table else None
            columns = header.find_all(["th", "td"], recursive=False) if header else []
            cells = box.find_all(["td", "th"], recursive=False)
            units = [i for i, col in enumerate(columns) if UNIT_COLUMN.fullmatch(_text(col).rstrip(":"))]
            if not units:
                continue
            heading = (cells[units[0]] if len(units) == 1 and len(columns) == len(cells)
                       and not any(c.has_attr("colspan") or c.has_attr("rowspan") for c in columns + cells) else None)
        else:
            continue
        label = _text(heading)
        if UNIT_LABEL.search(label):
            return "subunit"
        if INSTITUTION_LABEL.search(label):
            return None
        # A position in an unnamed or unfamiliar management body establishes the raw
        # role, but its jurisdiction requires checking the linked HTML.
        return "scope_review"
    return None


def _semantic_cards(soup, known, section):
    """Ascend from a role, stopping before another employee or a listing boundary."""
    for text in soup.find_all(string=ROLE_WORD):
        node = text.parent
        if isinstance(text, Comment) or len(str(text)) > 145 or not _role(str(text)) or _ignored(node, known):
            continue
        for root in (node, *node.parents):
            if root.name in {"html", "body", "main", "[document]", "ul", "ol", "table", "tbody"}:
                break
            if len(root.get_text(" ", strip=True)) > 1800:
                break
            names = _name_nodes(root)
            if len(names) > 1:
                break
            if names:
                name, name_node = next(iter(names.items()))
                # A bare two-word heading is easy to mistake for a person.
                if len(name.split()) < 3 and not root.select_one('[itemprop="name"]') and "." not in name:
                    break
                role_nodes = [t for t in root.find_all(string=ROLE_WORD) if _role(str(t))]
                if len(role_nodes) != 1:
                    break
                if SIGNATURE_LABEL.search(_text(root)) or re.search(r"с уважением|обращение директора", _text(root), re.I):
                    break
                # The image is often a sibling of the text column, still within one card.
                parent = root.parent
                if (not root.find("img") and parent and root.name not in {"li", "tr", "article"}
                        and not re.search(r"(?:^|[-_ ])card(?:$|[-_ ])", " ".join(root.get("class", [])))
                        and parent.name in {"div", "section", "article", "li"}
                        and len(parent.find_all("img", limit=2)) == 1 and len(_name_nodes(parent)) == 1
                        and len([t for t in parent.find_all(string=ROLE_WORD) if _role(str(t))]) == 1
                        and _text(root) == _text(parent)):
                    root = parent
                yield root, name_node, node, _role(str(text))
                break
            if root.name in {"li", "article", "tr"} or any(c.endswith("-card") or c == "card" for c in root.get("class", [])):
                break


def _labeled(scope, label_pattern):
    """Find a value paired with an explicit caption in a local field container."""
    for label in scope.find_all(["h3", "h4", "h5", "h6", "dt", "th", "label", "b", "strong", "div", "span", "p"]):
        if not label.string:
            continue
        caption, sep, inline = _text(label).partition(":")
        if not label_pattern.fullmatch(caption.strip()):
            continue
        if sep and inline.strip():
            return label, inline.strip()
        for sibling in label.next_siblings:
            if isinstance(sibling, Comment):
                continue
            if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6", "dt", "th"}:
                break
            value = _text(sibling) if isinstance(sibling, Tag) else " ".join(str(sibling).split())
            if value:
                if len(value) <= 650:
                    return sibling if isinstance(sibling, Tag) else label.parent, value
                break
    return None, None


def _build(root, name_node, role_node, method, page_url, ids, profile_scope=None, role_value=None):
    evidence = []

    def add(field, node, value):
        if node is not None and value:
            evidence.append(ElementEvidence(field, _selector(node, ids), value))
        return value

    name = _name(_text(name_node))
    role = role_value or _text(role_node).strip(" ,;:") or None
    if not (name or _role(role or "")):
        return None
    add("card", root, name or role)
    add("name", name_node, name)
    add("role", role_node, role)
    photo = root.select_one(PHOTO)
    if photo is None:
        images = [img for img in root.find_all("img") if not re.search("icon|logo|social", " ".join(img.get("class", [])), re.I)]
        matched = [img for img in images if name and name.casefold() in img.get("alt", "").casefold()]
        photo = matched[0] if len(matched) == 1 else (images[0] if len(images) == 1 else None)
    photo_url = None
    photo_state = "absent"
    if photo:
        photo_url = _url(page_url, photo.get("data-src") or photo.get("src") or "")
        hint = " ".join([unquote(urlsplit(photo_url).path) if photo_url else "", photo.get("alt", ""), *photo.get("class", [])])
        if PLACEHOLDER.search(hint):
            photo_state = "placeholder"
        elif photo_url:
            photo_state = "linked"
        add("photo", photo, photo_url or photo.get("alt"))

    contacts = {}
    for field, scheme, caption, valid in (
        ("email", "mailto:", r"(?:электронная почта|эл\.\s*почта|e-?mail)", MAIL),
        ("phone", "tel:", r"(?:телефон|контактный телефон)", PHONE),
    ):
        link = root.select_one(f'a[href^="{scheme}" i]')
        if link:
            value = _text(link)
            if not valid.search(value):
                value = unquote(link["href"][len(scheme):].split("?", 1)[0]).strip()
            item_node = link
        else:
            item_node, value = _labeled(root, re.compile(caption, re.I))
        value = valid.search(value) if value else None
        contacts[field] = add(field, item_node, value[0].strip() if value else None)

    edu = root.select_one('[itemprop="educationLevel"]')
    edu_value = _text(edu) or None
    if not edu_value:
        edu, edu_value = _labeled(profile_scope if profile_scope is not None else root, EDUCATION)
    if not edu_value and root.name == "tr":
        table = root.find_parent("table")
        header = table.find("tr") if table else None
        if header:
            columns = header.find_all(["th", "td"], recursive=False)
            cells = root.find_all(["th", "td"], recursive=False)
            if len(columns) == len(cells) and not any(c.has_attr("colspan") for c in columns + cells):
                for column, cell in zip(columns, cells):
                    if EDUCATION.fullmatch(_text(column).rstrip(":")):
                        edu, edu_value = cell, _text(cell) or None
                        break
    add("education", edu, edu_value)
    profile_link = root if root.name == "a" else None
    if profile_link is None and name_node:
        profile_link = name_node if name_node.name == "a" else name_node.find("a", href=True)
    if profile_link is None:
        profile_link = root.select_one(".gw-person-card__image[href], .gw-personal-card__image-link[href]")
    href = profile_link.get("href", "") if profile_link else ""
    profile_url = _url(page_url, href) if href and not href.startswith("#") else None
    if not profile_url and any(c in {"gw-person-detail", "gw-employee-info"} for c in root.get("class", [])):
        profile_url = page_url
    is_head = bool(role and LEADER.fullmatch(role))
    scope = _governance_scope(role_node) if is_head else None
    if scope:
        method = f"{method}_{scope}"
    return StaffCard(name, role, is_head and not scope, photo_url, photo_state,
                     edu_value, contacts["email"], contacts["phone"], profile_url, method, tuple(evidence))


def extract_staff(html: str, page_url: str, section: str = "home") -> list[StaffCard]:
    """Return HTML evidence, including incomplete cards. No network or database IO."""
    soup = BeautifulSoup(html, "html.parser")
    ids = Counter(tag["id"] for tag in soup.find_all(id=True))
    cards = []
    known = set()
    roots = soup.select(CARDS)
    for root in roots:
        known.add(id(root))
        if _ignored(root, set()):
            continue
        scope = root.find_parent("main") if len(roots) == 1 and "gw-person-detail" in root.get("class", []) else None
        name_node, role_node = root.select_one(NAME), root.select_one(ROLE)
        method, role_value = "template", None
        # A CMS update may keep the card boundary while renaming its inner fields.
        if not _name(_text(name_node)):
            names = _name_nodes(root)
            if len(names) == 1:
                name_node = next(iter(names.values()))
                method = "semantic"
        if not _text(role_node):
            role_texts = [s for s in root.find_all(string=ROLE_WORD) if _role(str(s)) and not _ignored(s.parent, set())]
            if len({str(s).strip() for s in role_texts}) == 1:
                role_node = role_texts[0].parent
                role_value = _role(str(role_texts[0]))
                method = "semantic"
        card = _build(root, name_node, role_node, method, page_url, ids, scope, role_value)
        if card:
            cards.append(card)
    if section not in {"home", "about", "information", "management", "staff", "team", "profile", "contacts"}:
        return cards
    for root, name_node, role_node, role_value in _semantic_cards(soup, known, section):
        if id(root) not in known:
            known.add(id(root))
            card = _build(root, name_node, role_node, "semantic", page_url, ids, role_value=role_value)
            if card:
                cards.append(card)
    return cards

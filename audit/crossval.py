"""Перекрёстные проверки между сайтами: эталон, сравнение документов, картинки-заглушки, рейтинг."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from . import config as C
from .models import Finding, SiteResult

# Дополнительные (необязательные) категории документов — по ним сайты сравниваются между собой.
EXTRA_DOC_CATEGORIES: dict[str, tuple[str, str]] = {
    "anticorr": ("Документы по противодействию коррупции", r"коррупц"),
    "pitanie": ("Документы об организации питания (меню, положение)", r"питани|меню"),
    "dostupnaya_sreda": ("Паспорт доступности / доступная среда", r"доступн\w+\s+сред|паспорт\w*\s+доступност"),
    "programma_razvitiya": ("Программа развития", r"программ\w+\s+развити"),
    "nastavnichestvo": ("Положение / программа наставничества", r"наставничеств"),
    "antiterror": ("Антитеррористическая безопасность", r"антитеррор|терроризм|паспорт\w*\s+безопасност"),
    "sovet_roditeley": ("Положение о совете родителей", r"совет\w*\s+родител|родительск\w+\s+(совет|комитет)"),
    "pedsovet": ("Положение о педагогическом совете", r"педагогическ\w+\s+совет"),
    "pers_dannye": ("Положение о защите персональных данных", r"персональн\w+\s+данн"),
    "sout": ("Специальная оценка условий труда (СОУТ)", r"СОУТ|специальн\w+\s+оценк\w+\s+услови"),
    "uchebniki": ("Перечень учебников", r"учебник"),
    "gia": ("Документы по ГИА", r"\bГИА\b|ОГЭ|ЕГЭ"),
    "adapt": ("Адаптированные образовательные программы", r"адаптированн\w+\s+(основн\w+\s+)?образовательн"),
    "dop_obr": ("Программы дополнительного образования", r"дополнительн\w+\s+(общеобразовательн|общеразвивающ|образовани)"),
    "eios": ("Положение об электронной информационно-образовательной среде", r"информационно[-\s]образовательн\w+\s+сред"),
    "odezhda": ("Положение о школьной форме / требованиях к одежде", r"(школьн\w+\s+форм|требовани\w+\s+к\s+одежд)"),
}


def site_doc_set(r: SiteResult) -> set[str]:
    titles = "\n".join(d.title for d in r.documents)
    keys = set(r.doc_categories)
    for k, (_, rx) in EXTRA_DOC_CATEGORIES.items():
        if re.search(rx, titles, re.I):
            keys.add(k)
    return keys


def doc_label(key: str) -> str:
    if key in C.REQUIRED_DOCS:
        return C.REQUIRED_DOCS[key][0]
    return EXTRA_DOC_CATEGORIES[key][0]


def mark_stub_photos(results: list[SiteResult], min_sites: int = 3) -> int:
    """Одинаковая картинка у персон на ≥3 разных сайтах — это заглушка шаблона, а не фото."""
    sites_by_hash: dict[str, set[str]] = defaultdict(set)
    for r in results:
        for p in r.persons:
            if p.photo_hash:
                sites_by_hash[p.photo_hash].add(r.domain)
    stubs = {h for h, s in sites_by_hash.items() if len(s) >= min_sites} | C.KNOWN_STUB_PHOTO_HASHES
    # одинаковая картинка у ≥3 разных людей одного сайта — тоже заглушка
    for r in results:
        names_by_hash = defaultdict(set)
        for p in r.persons:
            if p.photo_hash:
                names_by_hash[p.photo_hash].add(p.name)
        stubs |= {h for h, n in names_by_hash.items() if len(n) >= 3}
    for r in results:
        affected = defaultdict(list)
        for p in r.persons:
            if p.photo_hash in stubs:
                affected[p.list_page].append(p.name)
        for page, names in affected.items():
            names = sorted(set(names))
            r.add(Finding("Педагогический состав / коллектив",
                          f"Вместо фотографии у {len(names)} сотрудников стоит одинаковая картинка "
                          f"(силуэт-заглушка, клипарт или логотип)", "high",
                          "Разместить портретные фото сотрудников (деловой стиль, нейтральный фон) "
                          "или скрыть блок с фото.", page, names[:12],
                          "photo_stub"))
    return len(stubs)


def compute_score(r: SiteResult) -> None:
    if r.status != "ok":
        r.score, r.grade, r.grade_css = 0, "Недоступен", "bad"
        return
    per_section: dict[str, int] = defaultdict(int)
    for f in r.findings:
        per_section[f.section] += C.SEVERITY_WEIGHT[f.severity]
    penalty = sum(min(v, C.SECTION_PENALTY_CAP) for v in per_section.values())
    # плавная шкала: 10 штрафных баллов ≈ 88, 30 ≈ 69, 60 ≈ 47, 100 ≈ 29
    r.score = round(100 * math.exp(-penalty / 80))
    r.metrics["penalty"] = penalty
    for threshold, grade, css in C.GRADES:
        if r.score >= threshold:
            r.grade, r.grade_css = grade, css
            break


def choose_reference(results: list[SiteResult]) -> dict[str, SiteResult]:
    """Эталон для каждого типа учреждений — доступный сайт с наивысшим баллом
    (при равенстве — без критичных замечаний, затем с большим числом документов и новостей)."""
    refs = {}
    for kind in ("school", "kindergarten"):
        cands = [r for r in results if r.kind == kind and r.status == "ok"]
        if not cands:
            continue
        best = max(cands, key=lambda r: (r.score, -r.count("critical"), -r.count("high"),
                                         r.metrics.get("docs_total", 0), r.metrics.get("news_last_year", 0)))
        best.is_reference = True
        refs[kind] = best
    return refs


def compare_with_reference(results: list[SiteResult], refs: dict[str, SiteResult]) -> None:
    for kind, ref in refs.items():
        peers = [r for r in results if r.kind == kind and r.status == "ok"]
        ref_docs = site_doc_set(ref)
        freq = Counter(k for r in peers for k in site_doc_set(r))
        for r in peers:
            if r is ref:
                continue
            mine = site_doc_set(r)
            # обязательные документы уже отмечены отдельно — здесь только дополнительные категории
            gaps = [k for k in ref_docs - mine
                    if k in EXTRA_DOC_CATEGORIES and freq[k] / len(peers) >= 0.5]
            if gaps:
                r.add(Finding("Сравнение с эталоном",
                              f"Нет документов, которые есть у эталонного сайта и у большинства учреждений ({len(gaps)})",
                              "low", f"Сверить перечень документов с эталоном: {ref.domain}",
                              f"https://{ref.domain}/svedeniya-ob-obrazovatelnoy-organizatsii/dokumenty/",
                              [f"{doc_label(k)} — есть у {freq[k]} из {len(peers)} сайтов" for k in gaps],
                              "ref_docs"))
            missing_sections = [ref_key for ref_key in ref.sections if ref_key not in r.sections]
            if missing_sections:
                names = [C.SECTION_RULES[k][0] for k in missing_sections if k in C.SECTION_RULES]
                r.add(Finding("Сравнение с эталоном",
                              "Нет разделов, которые есть у эталонного сайта: " + ", ".join(names), "low",
                              f"Сравнить структуру сайта с эталоном: {ref.domain}", f"https://{ref.domain}/",
                              [], "ref_sections"))
            r.metrics["ref_domain"] = ref.domain
            r.metrics["ref_score_gap"] = ref.score - r.score
        ref.metrics["ref_domain"] = ref.domain


def finalize(results: list[SiteResult]) -> dict:
    stubs = mark_stub_photos(results)
    for r in results:
        compute_score(r)
    refs = choose_reference(results)
    compare_with_reference(results, refs)
    for r in results:          # пересчёт с учётом сравнения с эталоном
        compute_score(r)
    for r in refs.values():
        r.is_reference = True
    # частые проблемы по региону
    common = Counter()
    labels = {}
    for r in results:
        for f in {f.check: f for f in r.findings}.values():
            if f.check:
                common[f.check] += 1
                labels.setdefault(f.check, (f.section, f.problem, f.severity))
    top = [{"check": k, "sites": n, "section": labels[k][0], "example": labels[k][1], "severity": labels[k][2]}
           for k, n in common.most_common(15)]
    return {"references": {k: v.domain for k, v in refs.items()}, "stub_hashes": stubs, "common_problems": top}

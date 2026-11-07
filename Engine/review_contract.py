"""Build page-scoped review requests and validate an independent reviewer's JSON response."""

import argparse
from copy import deepcopy
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


IDENTITY = ("organization_id", "run_id", "page_id")
VISUAL_KINDS = {"image", "viewport", "full", "pdf"}
TEXT_KINDS = {"html", "dom", "external_excerpt", "timeline_record"}
EVIDENCE_SOURCES = {"page_text", "page_pdf", "page_screen", "attachment", "print_comparison","publication_sources"}
OUTCOMES = ("present", "absent", "uncertain")
EVIDENCE_LIMIT = 600
RESULT_FIELDS = {"check_id", "outcome", "material_ids", "evidence"}


def _object(value, fields, name, *, optional=()):
    if not isinstance(value, dict) or not fields <= value.keys() or value.keys() - fields - set(optional):
        raise ValueError(f"Некорректные поля объекта {name}.")


def _string(value, name, maximum=None):
    if not isinstance(value, str) or not value.strip() or (maximum is not None and len(value) > maximum):
        raise ValueError(f"{name}: требуется непустая строка" + (f" длиной до {maximum} символов." if maximum else "."))
    return value


def _id(value, name):
    if _string(value, name, 128) != value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError(f"Некорректный идентификатор {name}.")
    return value


def _identity(value):
    _object(value, set(IDENTITY), "identity")
    if any(type(value[k]) is not int or value[k] < 1 for k in IDENTITY):
        raise ValueError("В identity требуются три положительных целочисленных ID.")


def _strings(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name}: требуется массив строк.")
    for item in value:
        _id(item, name)
    if len(set(value)) != len(value):
        raise ValueError(f"{name}: повторяющиеся идентификаторы.")


def _category(kind):
    if kind == "guide":
        return "reference"
    if kind == "attachment":
        return "file"
    if kind in VISUAL_KINDS:
        return "visual"
    if kind in TEXT_KINDS:
        return "text"
    return None


def _path(value, page_id):
    value = _string(value, "material.path")
    parts = value.replace("\\", "/").split("/")
    if (any(p in {"", ".", ".."} for p in parts) or PureWindowsPath(value).drive
            or re.search(r"[:\x00-\x1f\x7f]", value)):
        raise ValueError("material.path должен быть относительным путём внутри пакета.")
    # The review exporter uses pages/<page_id>/ for page-specific files and shares content-addressed assets.
    if parts[0] == "pages" and len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) != page_id:
        raise ValueError("Материал из каталога другой страницы.")
    return "/".join(parts)


def make_request(identity: dict, checks: list[dict], materials: list[dict]) -> dict:
    """Default to the allowed material_kinds when supplied, otherwise visual evidence."""
    _identity(identity)
    if not isinstance(checks, list) or not checks or not isinstance(materials, list):
        raise ValueError("Нужны непустой массив checks и массив materials.")
    normalized_checks = []
    for check in checks:
        _object(check, {"id", "instruction"}, "check", optional=("kind", "material_kinds","visual","attachment","evidence_policy","publication"))
        if "publication" in check:
            pub=check["publication"]
            scope=pub.get("scope") if isinstance(pub,dict) else None
            if scope not in {"event","participation","internal"}:
                raise ValueError("Не задана область проверки публикации.")
            if scope!="internal" and (type(pub.get("minimum_sources"))!=int or pub["minimum_sources"]<2 or
                 type(pub.get("factcheck_id"))!=int or not pub.get("allowlist_fingerprint")):
                raise ValueError("Требуются порог независимых издателей и сохранённая политика источников.")
        if "attachment" in check:
            attachment = check["attachment"]
            if not isinstance(attachment, dict) or type(attachment.get("id")) is not int or attachment["id"] <= 0:
                raise ValueError("Нужен ID вложения для этой проверки.")
        if "evidence_policy" in check:
            policy = check["evidence_policy"]
            _object(policy, {"sources","aggregation"}, "evidence_policy")
            _strings(policy["sources"], "evidence_policy.sources")
            if (not policy["sources"] or set(policy["sources"])-EVIDENCE_SOURCES
                or policy["aggregation"] not in {"exists","all","consensus"}):
                raise ValueError("Неизвестный источник или режим объединения частей материала.")
        if "visual" in check:
            visual = check["visual"]
            if not isinstance(visual,dict) or not isinstance(visual.get("target"),str):
                raise ValueError("Не указана цель визуальной проверки.")
        _id(check["id"], "check.id")
        _string(check["instruction"], "check.instruction")
        kind = check.get("kind", "any" if "material_kinds" in check else "visual")
        if kind not in ("visual", "text", "any"):
            raise ValueError("check.kind должен быть visual, text или any.")
        if "material_kinds" in check:
            _strings(check["material_kinds"], "check.material_kinds")
            if set(check["material_kinds"]) - VISUAL_KINDS - TEXT_KINDS - {"attachment"}:
                raise ValueError("Неизвестный тип в check.material_kinds.")
        normalized_checks.append({**deepcopy(check), "kind": kind})
    _strings([check["id"] for check in checks], "check.id")

    normalized_materials = []
    for material in materials:
        if not isinstance(material, dict) or not {"id", "kind", "path"} <= material.keys():
            raise ValueError("Для каждого материала нужны id, kind и path.")
        _id(material["id"], "material.id")
        _id(material["kind"], "material.kind")
        if "for_checks" in material:
            _strings(material["for_checks"],"material.for_checks")
        if not _category(material["kind"]):
            raise ValueError("Неизвестный тип материала проверки.")
        for key in IDENTITY:
            if key in material and (type(material[key]) is not int or material[key] != identity[key]):
                raise ValueError(f"Материал относится к другой записи по {key}.")
        if "identity" in material:
            _identity(material["identity"])
            if material["identity"] != identity:
                raise ValueError("Материал относится к другому снимку.")
        path = _path(material["path"], identity["page_id"])
        if _category(material["kind"]) == "visual" and PurePosixPath(path).suffix.lower() in {
                ".htm", ".html", ".txt", ".json", ".csv", ".md"}:
            raise ValueError("Текстовый файл указан как визуальный материал.")
        normalized_materials.append({**deepcopy(material), "path": path})
    _strings([item["id"] for item in materials], "material.id")
    request = {"identity": dict(identity), "checks": normalized_checks, "materials": normalized_materials}
    try:
        json.dumps(request, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Метаданные запроса должны быть представимы в JSON.") from exc
    return request


def _request(request):
    _object(request, {"identity", "checks", "materials"}, "request")
    return make_request(request["identity"], request["checks"], request["materials"])


def allowed_material_ids(check, materials):
    kinds = check.get("material_kinds")
    category = check.get("kind","any")
    return [item["id"] for item in materials if item["kind"] != "guide"
            and (item["kind"] not in {"external_excerpt","timeline_record"} or
                 check.get("publication") and check["id"] in item.get("for_checks",[]))
            and (not check.get("visual") and "for_checks" not in item or check["id"] in item.get("for_checks",[]))
            and (not check.get("attachment") or (item["kind"]=="attachment"
                 and item.get("attachment_id")==check["attachment"]["id"]))
            and (kinds is None or item["kind"] in kinds)
            and (category == "any" or _category(item["kind"]) == category)]


def outcomes_for(check):
    return (*OUTCOMES,"not_applicable") if check.get("visual",{}).get("conditional") else OUTCOMES


def _schema_object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def schema_for(request) -> dict:
    """JSON Schema 2020-12. Results may be returned in any order, exactly once per check."""
    request = _request(request)
    variants, required_checks = [], []
    for check in request["checks"]:
        allowed = allowed_material_ids(check, request["materials"])
        publication=check.get("publication",{})
        options=list(outcomes_for(check)) if allowed else ["uncertain"]
        if publication.get("scope") in {"event","participation"}:
            options=["present","uncertain"] if len(allowed)>=publication["minimum_sources"] else ["uncertain"]
        fields = _schema_object({
            "check_id": {"const": check["id"]},
            "outcome": {"enum":options},
            "material_ids": {"type": "array", "items": {"enum": allowed} if allowed else False,
                             "uniqueItems": True, "maxItems": len(allowed)},
            "evidence": {"type": "string", "minLength": 1, "maxLength": EVIDENCE_LIMIT, "pattern": r"\S"},
        })
        needed = len(allowed) if check.get("visual",{}).get("target")=="staff_series" else 1
        if publication:
            needed=publication.get("minimum_sources",2)
        fields.update({"if": {"properties": {"outcome": {"enum": ["present", "absent", "not_applicable"]}}},
                       "then": {"properties": {"material_ids": {"minItems": needed}}}})
        variants.append(fields)
        required_checks.append({"contains": {"properties": {"check_id": {"const": check["id"]}}},
                                "minContains": 1, "maxContains": 1})
    fields = _schema_object({
        "identity": _schema_object({key: {"type": "integer", "const": value}
                                    for key, value in request["identity"].items()}),
        "results": {"type": "array", "minItems": len(variants), "maxItems": len(variants),
                    "items": {"oneOf": variants}, "allOf": required_checks},
    })
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **fields}


def validate_response(request, response, *, part=False) -> None:
    request = _request(request)
    _object(response, {"identity", "results"}, "response")
    _identity(response["identity"])
    if response["identity"] != request["identity"]:
        raise ValueError("Ответ относится к другой организации, обходу или странице.")
    checks = {c["id"]: c for c in request["checks"]}
    results = response["results"]
    if not isinstance(results, list) or len(results) != len(checks):
        raise ValueError("Требуется по одному результату для каждой проверки.")
    seen = set()
    for row in results:
        _object(row, RESULT_FIELDS, "result")
        check_id = _id(row["check_id"], "result.check_id")
        if check_id not in checks or check_id in seen:
            raise ValueError(f"Неизвестная или повторная проверка {check_id}.")
        seen.add(check_id)
        if row["outcome"] not in outcomes_for(checks[check_id]):
            raise ValueError(f"Некорректный исход в проверке {check_id}.")
        _string(row["evidence"], f"evidence для {check_id}", EVIDENCE_LIMIT)
        references = row["material_ids"]
        _strings(references, "result.material_ids")
        allowed = allowed_material_ids(checks[check_id], request["materials"])
        if set(references).difference(allowed):
            raise ValueError(f"Для {check_id} указан посторонний материал или неподходящий тип.")
        if row["outcome"] != "uncertain" and not references:
            raise ValueError(f"Для определённого результата {check_id} нужен подходящий материал.")
        pub=checks[check_id].get("publication")
        if pub and not part and row["outcome"]!="uncertain":
            cited=[m for m in request["materials"] if m["id"] in references]
            if pub["scope"]=="internal":
                if {m.get("record_type") for m in cited}!={"event","article"}:
                    raise ValueError("Для сравнения требуются карточка мероприятия и полная новость.")
            else:
                if __package__:
                    from .publication_review import independent_reports
                else:
                    from publication_review import independent_reports
                if row["outcome"]!="present" or len(independent_reports(checks[check_id],cited))<pub["minimum_sources"]:
                    raise ValueError("Утверждение не подтверждено требуемым числом независимых издателей.")
        if (row["outcome"] != "uncertain" and checks[check_id].get("visual",{}).get("target")=="staff_series"
            and set(references)!=set(allowed)):
            raise ValueError("Сравнение серии требует ссылки на каждый показанный портрет.")


def loads_strict(text):
    """Decode a standard JSON document with unambiguous field names at every level."""
    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError(f"Повторяющееся поле JSON: {name}.")
            result[name] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Недопустимое число JSON: {value}.")

    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)


def _load(path):
    return loads_strict(Path(path).read_text(encoding="utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Сверить JSON-ответ с запросом проверки сохранённой страницы.")
    parser.add_argument("request", help="JSON-файл запроса")
    parser.add_argument("response", help="JSON-файл ответа")
    args = parser.parse_args(argv)
    try:
        validate_response(_load(args.request), _load(args.response))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Ответ не принят: {exc}\n")
    print("Ответ соответствует запросу.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import json
import unittest
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from review_contract import EVIDENCE_LIMIT, main, make_request, schema_for, validate_response


class ReviewContractTest(unittest.TestCase):
    def setUp(self):
        self.identity = {"organization_id": 2, "run_id": 19, "page_id": 47}
        self.checks = [{"id": "director", "instruction": "Есть ли фотография директора?", "material_kinds": ["image"]},
                       {"id": "contact", "instruction": "Указан адрес?", "material_kinds": ["dom", "html"]}]
        self.materials = [{"id": "source", "kind": "html", "path": "pages/47/source.html"},
                          {"id": "portrait", "kind": "image", "path": "assets/portrait.jpg"}]
        self.request = make_request(self.identity, self.checks, self.materials)
        self.response = {"identity": self.identity, "results": [
            {"check_id": "contact", "outcome": "absent", "material_ids": ["source"], "evidence": "Адрес в HTML не указан."},
            {"check_id": "director", "outcome": "present", "material_ids": ["portrait"], "evidence": "В кадре есть человек."},
        ]}

    def test_valid_unordered_result_and_draft_schema(self):
        self.assertIsNone(validate_response(self.request, self.response))
        schema = schema_for(self.request)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        results = schema["properties"]["results"]
        self.assertEqual(results["minItems"], results["maxItems"])
        self.assertEqual({node["contains"]["properties"]["check_id"]["const"] for node in results["allOf"]},
                         {"contact", "director"})
        director = results["items"]["oneOf"][0]
        self.assertEqual(director["properties"]["material_ids"]["items"]["enum"], ["portrait"])
        self.assertEqual(director["properties"]["evidence"]["maxLength"], EVIDENCE_LIMIT)
        self.assertNotIn("kind", self.checks[0])

    def test_required_once_with_exact_identity_and_cited_materials(self):
        wrong_rows = [dict(check_id="missing"), dict(check_id="contact"), dict(outcome="yes"), dict(extra=100),
                      dict(material_ids=["an_image_from_another_request"]), dict(material_ids=["source"]),
                      dict(material_ids=["portrait", "portrait"]), dict(material_ids=[]), dict(evidence="  "),
                      dict(evidence="Я" * (EVIDENCE_LIMIT + 1))]
        for change in wrong_rows:
            with self.subTest(change=change):
                response = deepcopy(self.response)
                response["results"][1].update(change)
                with self.assertRaises(ValueError):
                    validate_response(self.request, response)
        for field in self.identity:
            with self.subTest(field=field):
                response = deepcopy(self.response)
                response["identity"][field] += 1
                with self.assertRaises(ValueError):
                    validate_response(self.request, response)
        response = deepcopy(self.response)
        response["results"].pop()
        with self.assertRaises(ValueError):
            validate_response(self.request, response)

    def test_no_visual_evidence_allows_only_uncertain(self):
        request = make_request(self.identity, [{"id": "photo", "instruction": "Проверить портрет"}], self.materials[:1])
        row = {"check_id": "photo", "outcome": "uncertain", "evidence": "Снимок недоступен.", "material_ids": []}
        response = {"identity": self.identity, "results": [row]}
        validate_response(request, response)
        fields = schema_for(request)["properties"]["results"]["items"]["oneOf"][0]["properties"]
        self.assertEqual(fields["outcome"]["enum"], ["uncertain"])
        self.assertEqual(fields["material_ids"]["maxItems"], 0)
        for outcome, refs in [("absent", []), ("present", ["source"]), ("uncertain", ["source"])]:
            row.update(outcome=outcome, material_ids=refs)
            with self.assertRaises(ValueError):
                validate_response(request, response)

    def test_reject_bad_request_ids_or_other_page_material(self):
        for change in [dict(page_id=48), dict(identity={**self.identity, "run_id": 20}),
                       dict(path="pages/48/else.png"), dict(path="../outside.png"), dict(path="C:\\secret.png"),
                       dict(path="source.html"), dict(kind="made_up"), dict(id="source")]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                make_request(self.identity, self.checks, [self.materials[0], {**self.materials[1], **change}])
        for identity in [dict(self.identity, run_id=True), dict(self.identity, extra_id=12)]:
            with self.assertRaises(ValueError):
                make_request(identity, self.checks, [])
        with self.assertRaises(ValueError):
            make_request(self.identity, [self.checks[0], self.checks[0]], self.materials)

    def test_cli_file_pair_and_duplicate_json_field(self):
        paths = []
        for document in (self.request, self.response):
            with NamedTemporaryFile(mode="w", encoding="utf-8-sig", suffix=".json", delete=False) as target:
                json.dump(document, target, ensure_ascii=False)
            path = Path(target.name)
            self.addCleanup(path.unlink, missing_ok=True)
            paths.append(str(path))
        with redirect_stdout(StringIO()):
            self.assertEqual(main(paths), 0)
        Path(paths[1]).write_text('{"identity":{},"identity":{}}', encoding="utf-8")
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(paths)
        self.assertEqual(error.exception.code, 1)

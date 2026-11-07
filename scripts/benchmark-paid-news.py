#!/usr/bin/env python3
"""One bounded run against Blackroute using the crawler's request and parser."""
import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audit.llm import LLMSettings, OpenAICompatChecker, SCHEMA  # noqa: E402

GATEWAY = "https://blackroute.ironborn.cc/v1"
MODELS = {"flash": "gemini-2.5-flash", "lite": "gemini-2.5-flash-lite",
          "qwen": "qwen3-235b-a22b-instruct-2507-maas"}
DATASET = ROOT / "benchmarks/event_dates.json"
REFERENCE = ROOT / "benchmarks/results/qwen3.5-9b.json"
GROUND_CASES = ("school-05", "school-07")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def measure(checker, case, session, prices):
    row = {"id": case["id"], "origin": case["origin"], "expected": case["expected_event_date"]}
    def post(body):
        response = session.post(checker.url, headers=checker.headers, json=body, timeout=(10, 100),
                                allow_redirects=False)
        row["http_status"] = response.status_code
        if not response.ok:
            row["error"] = "HTTP " + str(response.status_code)
            return None
        payload = response.json()
        usage = payload.get("usage", {})
        row["usage"] = usage
        row["cost_headers"] = {k: v for k, v in response.headers.items()
                               if k.lower() in ("x-cost-usd", "x-request-cost", "x-request-cost-usd")}
        if "prompt_tokens" in usage and "completion_tokens" in usage:
            cost = (Decimal(prices["prompt"]) * usage["prompt_tokens"]
                    + Decimal(prices["completion"]) * usage["completion_tokens"]
                    + Decimal(prices["request"]))
            row["token_tariff_usd"] = str(cost)
        row["returned_model"] = payload.get("model")
        choice = (payload.get("choices") or [{}])[0]
        row["finish_reason"] = choice.get("finish_reason")
        message = choice.get("message", {})
        try:
            content = json.loads(message.get("content") or "")
        except ValueError:
            content = None
        row["raw_event_date_present"] = isinstance(content, dict) and "event_date" in content
        if row["raw_event_date_present"]:
            row["raw_event_date"] = content["event_date"]
        row["annotations"] = [a["url_citation"] for a in message.get("annotations") or []
                              if a.get("type") == "url_citation"]
        return payload

    # Single attempt per article. The crawler still builds and parses its normal request.
    checker._post = post
    start = time.monotonic()
    try:
        result = checker.check_news(case["title"], case["text"],
                                    datetime.fromisoformat(case["published"]), case["site_name"])
    except requests.RequestException as exc:
        result = None
        row["error"] = type(exc).__name__
    row["seconds"] = round(time.monotonic() - start, 3)
    row["response"] = result
    if result is None:
        row.setdefault("error", "No parsed response")
    row["predicted"] = result["event_date"].isoformat() if result and result["event_date"] else None
    row["correct"] = result is not None and row["predicted"] == row["expected"]
    return row


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("model", choices=MODELS)
    cli.add_argument("--through", type=int, default=30, choices=range(1, 31), metavar="1..30")
    cli.add_argument("--grounded", action="store_true", help="Two separate Google Search examples with Flash")
    args = cli.parse_args()
    if subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip() != "AI-only-solution":
        cli.error("Use AI-only-solution")
    if args.grounded and args.model != "flash":
        cli.error("Only Flash supports this search test")
    reference = json.loads(REFERENCE.read_text())
    dataset_hash = sha(DATASET.read_bytes())
    if dataset_hash != reference["dataset_sha256"] or sha(reference["system_prompt"].encode()) != reference["system_sha256"]:
        cli.error("Local reference and dataset hashes must match")
    mode = "google" if args.grounded else "off"
    cases = json.loads(DATASET.read_text())["cases"]
    if args.grounded:
        cases = [case for case in cases if case["id"] in GROUND_CASES]
    cases = cases[:args.through]
    model = MODELS[args.model]
    file = ROOT / "benchmarks/results" / f"paid-{args.model}-{mode}.json"
    saved = json.loads(file.read_text()) if file.exists() else None
    key = next(line.split("=", 1)[1].strip().strip('\"').strip("'")
               for line in (ROOT / ".env").read_text().splitlines() if line.startswith("AUDIT_LLM_API_KEY="))
    with requests.Session() as session, TemporaryDirectory() as cache:
        os.environ["AUDIT_CACHE_DIR"] = cache
        checker = OpenAICompatChecker(LLMSettings(provider="openai", base_url=GATEWAY,
                    api_key=key, model=model, web_search=mode, json_mode="json_schema", max_tokens=2048))
        if not args.grounded:
            checker.system = reference["system_prompt"]
        info = session.get(GATEWAY + "/models", headers=checker.headers, timeout=20, allow_redirects=False)
        info.raise_for_status()
        prices = next(m["pricing"] for m in info.json()["data"] if m["id"] == model)
        # Conservative token budget per offline model (input byte length exceeds token count).
        sample_body = checker._body(json.dumps(SCHEMA, ensure_ascii=False))
        ceiling = sum((len(json.dumps(sample_body, ensure_ascii=False).encode()) + len(c["text"].encode())
                       + len(c["title"].encode()) + len(c["site_name"].encode()) + 1000) * Decimal(prices["prompt"])
                      + 2048 * Decimal(prices["completion"]) + Decimal(prices["request"]) for c in cases)
        if ceiling > Decimal("0.30"):
            cli.error("Token estimate exceeds $0.30 per model; stopped before inference")
        run = saved or {"model": model, "gateway": GATEWAY, "started": datetime.now().astimezone().isoformat(),
                        "dataset_sha256": dataset_hash, "system_sha256": sha(checker.system.encode()),
                        "web_search": mode, "prices_usd_per_token": prices, "max_tokens": 2048, "rows": []}
        if (run["model"], run["dataset_sha256"], run["system_sha256"], run["prices_usd_per_token"]) != (
                model, dataset_hash, sha(checker.system.encode()), prices):
            cli.error("Saved run has a different configuration")
        rows = run["rows"]
        if rows and [r["id"] for r in rows] != [c["id"] for c in cases[:len(rows)]]:
            cli.error("Requested range does not extend the existing run")
        if rows and rows[-1].get("error"):
            cli.error("Saved last request failed; inspect before continuing")
        for case in cases[len(rows):]:
            row = measure(checker, case, session, prices)
            rows.append(row)
            tmp = file.with_suffix(".tmp")
            tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2, default=str) + "\n")
            tmp.replace(file)
            print(model, len(rows), row["id"], row.get("http_status"), row["seconds"],
                  row.get("token_tariff_usd", "-"), row.get("error", "ok"), flush=True)
            if row.get("error"):
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

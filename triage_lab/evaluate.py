"""Run a measured, label-isolated incident evaluation and retain every prediction."""

import argparse
import asyncio
import hashlib
import json
import platform
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import numpy as np

from triage_lab.config import Settings, get_retriever
from triage_lab.graph import TriageAgent
from triage_lab.mcp_client import http_backend, stdio_backend
from triage_lab.models import Alert
from triage_lab.observability import SnapshotBackend
from triage_lab.scoring import score_case


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@asynccontextmanager
async def backend_for(transport, cases, settings):
    if transport == "snapshot":
        yield SnapshotBackend({row["id"]: row["snapshot"] for row in cases})
    elif transport == "stdio":
        with tempfile.TemporaryDirectory(prefix="triage-eval-") as directory:
            path = Path(directory) / "observations.jsonl"
            path.write_text("\n".join(json.dumps({"id": row["id"], "snapshot": row["snapshot"]})
                                      for row in cases) + "\n")
            async with stdio_backend(str(path)) as backend:
                yield backend
    else:
        async with http_backend(settings.mcp_url) as backend:
            yield backend


def summarize(records):
    scores = [row["scores"] for row in records]
    predictions = [row["diagnosis"] for row in records]
    n = len(records)
    retrieval = [row["retrieval_hit_at_3"] for row in scores if row["retrieval_hit_at_3"] is not None]
    abstain_tp = sum(row["expected_abstention"] and row["predicted_abstention"] for row in scores)
    expected_abstain = sum(row["expected_abstention"] for row in scores)
    predicted_abstain = sum(row["predicted_abstention"] for row in scores)
    citations = sum(row["evidence_count"] for row in scores)
    valid_citations = sum(row["valid_evidence_count"] for row in scores)
    costs = [row["usage"]["estimated_cost_usd"] for row in predictions]
    latencies = [row["elapsed_ms"] for row in predictions]
    by_cause = {}
    for row in records:
        group = by_cause.setdefault(row["expected"]["root_cause"], {"n": 0, "correct": 0})
        group["n"] += 1
        group["correct"] += row["scores"]["root_cause_correct"]
    return {
        "n": n,
        "root_cause_correct": sum(row["root_cause_correct"] for row in scores),
        "root_cause_accuracy": sum(row["root_cause_correct"] for row in scores) / n,
        "cause_and_service_accuracy": sum(row["root_cause_correct"] and row["affected_service_correct"]
                                          for row in scores) / n,
        "service_accuracy": sum(row["affected_service_correct"] for row in scores) / n,
        "runbook_hit_at_3": sum(retrieval) / len(retrieval) if retrieval else None,
        "runbook_denominator": len(retrieval),
        "citation_id_validity": valid_citations / citations if citations else None,
        "citation_count": citations,
        "required_sources_covered_rate": sum(row["required_sources_covered"] for row in scores) / n,
        "abstention_precision": abstain_tp / predicted_abstain if predicted_abstain else None,
        "abstention_recall": abstain_tp / expected_abstain if expected_abstain else None,
        "abstention_counts": {"true_positive": abstain_tp, "expected": expected_abstain,
                              "predicted": predicted_abstain},
        "latency_ms": {"p50": round(float(np.percentile(latencies, 50)), 3),
                       "p95": round(float(np.percentile(latencies, 95)), 3),
                       "mean": round(float(np.mean(latencies)), 3)},
        "mean_estimated_token_cost_usd": sum(costs) / n if all(c is not None for c in costs) else None,
        "total_input_tokens": sum(row["usage"]["input_tokens"] for row in predictions),
        "total_output_tokens": sum(row["usage"]["output_tokens"] for row in predictions),
        "tool_calls": sum(len(row["tool_trace"]) for row in predictions),
        "tool_errors": sum(call["status"] != "ok" for row in predictions for call in row["tool_trace"]),
        "by_cause": by_cause,
    }


async def evaluate(args):
    settings = Settings()
    if args.provider:
        settings.triage_provider = args.provider
    if args.mode:
        settings.retrieval_mode = args.mode
    cases = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]
    if args.limit:
        cases = cases[:args.limit]
    if not cases or len({row["id"] for row in cases}) != len(cases):
        raise ValueError("Dataset must be nonempty with unique incident IDs")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "predictions.jsonl").exists():
        raise FileExistsError("Choose a new output directory to preserve earlier measured runs")
    retriever = get_retriever(settings)
    records = []
    async with backend_for(args.transport, cases, settings) as backend:
        agent = TriageAgent(settings, backend, retriever)
        with (output / "predictions.jsonl").open("w") as stream:
            for index, case in enumerate(cases, 1):
                # Only Alert and read-only public observations cross into the agent.
                diagnosis = (await agent.diagnose(Alert.model_validate(case["alert"]))).model_dump()
                record = {"id": case["id"], "expected": case["expected"], "diagnosis": diagnosis,
                          "scores": score_case(case, diagnosis)}
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                records.append(record)
                if index % 30 == 0:
                    print(f"Evaluated {index}/{len(cases)}", flush=True)
    summary = {
        "included_frozen_test_corpus": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()
            == "65ecd627c2085fe019a7377d48b2f5045c6e82d8b226c4446772e8a936d3d36a",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.dataset),
        "dataset_sha256": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
        "source_sha256": fingerprint(Path("triage_lab").glob("*.py")),
        "runbooks_sha256": fingerprint(Path(settings.runbook_dir).glob("*.md")),
        "transport": args.transport, "provider": settings.triage_provider,
        "model": settings.model_name if settings.triage_provider == "compatible" else None,
        "retrieval": retriever.metadata,
        "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                    "langgraph": version("langgraph"), "mcp": version("mcp")},
        "measurement_scope": "Warm process, serial cases, full graph/tool/retrieval/model latency; excludes startup and indexing",
        "cost_scope": "Token charges only; excludes local compute, infrastructure, and subscription costs",
        "limitations": ["Synthetic templated incidents, not production incidents",
                        "Citation ID validity does not establish natural-language claim entailment",
                        "Baseline is an untrained rule system; local embedding/reranker are nonlearned",
                        "No confidence calibration or human remediation-quality review"],
        "metrics": summarize(records),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output / "results.md").write_text(render_results(summary))
    print(json.dumps(summary["metrics"], indent=2))
    return summary


def render_results(summary):
    m = summary["metrics"]
    def percent(value):
        return f"{value:.1%}" if value is not None else "N/A"
    cost = m["mean_estimated_token_cost_usd"]
    return (f"# Measured evaluation\n\n"
            f"Run completed {summary['completed_at_utc']}. Provider `{summary['provider']}`, "
            f"transport `{summary['transport']}`, {m['n']} synthetic incidents.\n\n"
            "| Metric | Measured result |\n|---|---:|\n"
            f"| Root-cause exact match | {percent(m['root_cause_accuracy'])} ({m['root_cause_correct']}/{m['n']}) |\n"
            f"| Cause and service correct | {percent(m['cause_and_service_accuracy'])} |\n"
            f"| Runbook hit@3 | {percent(m['runbook_hit_at_3'])} (n={m['runbook_denominator']}) |\n"
            f"| Citation ID validity | {percent(m['citation_id_validity'])} (n={m['citation_count']}) |\n"
            f"| Abstention precision / recall | {percent(m['abstention_precision'])} / {percent(m['abstention_recall'])} |\n"
            f"| Latency p50 / p95 | {m['latency_ms']['p50']:.3f} / {m['latency_ms']['p95']:.3f} ms |\n"
            f"| Mean token charge per query | {'unavailable' if cost is None else f'${cost:.8f}'} |\n\n"
            f"{summary['measurement_scope']}. {summary['cost_scope']}.\n\n"
            "These are synthetic lab results. Citation validity checks IDs and ownership, not prose entailment. "
            "Baseline results are not LLM results. See summary.json for provenance and per-cause breakdown.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="evals/incidents.jsonl")
    parser.add_argument("--provider", choices=("baseline", "compatible"))
    parser.add_argument("--transport", choices=("snapshot", "stdio", "http"), default="stdio")
    parser.add_argument("--mode", choices=("bm25", "dense", "hybrid", "hybrid_rerank"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", default="artifacts/evals/initial")
    asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    main()

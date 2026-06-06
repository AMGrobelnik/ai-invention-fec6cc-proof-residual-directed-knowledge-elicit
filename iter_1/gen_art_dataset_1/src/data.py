#!/usr/bin/env python3
"""Convert neuro-symbolic reasoning datasets to exp_sel_data_out schema."""

import json
import sys
from pathlib import Path
from loguru import logger

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/data.log", rotation="30 MB", level="DEBUG")

WORKSPACE = Path("/ai-inventor/aii_data/runs/b16e7/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")

# Max examples per dataset to include in data_out.json
MAX_PER_DATASET = 5000


def format_input(row: dict) -> str:
    """Format premises + hypothesis into a structured text input."""
    premises = row.get("premises", [])
    hypothesis = row.get("hypothesis", "")
    dataset = row.get("dataset", "")

    if dataset == "clutrr":
        # CLUTRR: story + kinship query
        story_text = " ".join(p.rstrip(".").strip() for p in premises if p.strip())
        return f"Story: {story_text}. Query: {hypothesis}"
    else:
        # FOLIO, ProofWriter, RuleTaker: premises as facts/rules + hypothesis as question
        if premises:
            premises_text = " ".join(p.strip() for p in premises if p.strip())
            return f"Context: {premises_text} Question: {hypothesis}"
        else:
            return f"Question: {hypothesis}"


def row_to_example(row: dict) -> dict:
    """Convert a unified-schema row to exp_sel_data_out example format."""
    input_text = format_input(row)
    output_text = str(row.get("label", ""))

    example = {
        "input": input_text,
        "output": output_text,
        "metadata_id": str(row.get("id", "")),
        "metadata_depth": int(row.get("depth", 0)),
        "metadata_split": str(row.get("split", "")),
        "metadata_gold_predicate_count": int(len(row.get("gold_predicates", []))),
        "metadata_task_type": "classification",
    }

    # Dataset-specific metadata
    dataset = row.get("dataset", "")
    meta = row.get("metadata", {}) or {}

    if dataset == "folio":
        example["metadata_n_classes"] = 3
        example["metadata_label_space"] = "True|False|Uncertain"
        if meta.get("story_id") is not None:
            example["metadata_story_id"] = str(meta["story_id"])
    elif dataset == "proofwriter":
        example["metadata_n_classes"] = 3
        example["metadata_label_space"] = "True|False|Unknown"
        example["metadata_config"] = str(meta.get("config", ""))
    elif dataset == "ruletaker":
        example["metadata_n_classes"] = 2
        example["metadata_label_space"] = "entailment|not entailment"
        example["metadata_config"] = str(meta.get("config", ""))
    elif dataset == "clutrr":
        example["metadata_n_classes"] = 20  # kinship relations
        example["metadata_label_space"] = "kinship_relation"
        example["metadata_f_comb"] = str(meta.get("f_comb", ""))
        example["metadata_task_name"] = str(meta.get("task_name", ""))

    return example


def load_and_convert(file_path: Path, dataset_name: str, max_rows: int = MAX_PER_DATASET) -> dict:
    """Load a full_*.json file and convert to dataset entry."""
    logger.info(f"Loading {dataset_name} from {file_path.name}...")
    rows = json.loads(file_path.read_text())

    # Cap rows
    if len(rows) > max_rows:
        # Stratified sample by label
        from collections import defaultdict
        import random
        random.seed(42)
        groups: dict = defaultdict(list)
        for r in rows:
            groups[r.get("label", "unknown")].append(r)
        sampled = []
        per_class = max_rows // len(groups)
        for cls_rows in groups.values():
            sampled.extend(random.sample(cls_rows, min(per_class, len(cls_rows))))
        # Fill remainder
        remainder = [r for r in rows if r not in sampled]
        random.shuffle(remainder)
        while len(sampled) < max_rows and remainder:
            sampled.append(remainder.pop())
        rows = sampled[:max_rows]
        logger.info(f"  Sampled {len(rows)} rows from {len(groups)} label classes")
    else:
        logger.info(f"  Using all {len(rows)} rows")

    examples = []
    for row in rows:
        try:
            ex = row_to_example(row)
            examples.append(ex)
        except Exception as e:
            logger.debug(f"  Skipped row {row.get('id', '?')}: {e}")

    logger.info(f"  {dataset_name}: {len(examples)} examples")
    return {"dataset": dataset_name, "examples": examples}


@logger.catch(reraise=True)
def main():
    logger.info("=== Building full_data_out.json ===")

    dataset_files = [
        (WORKSPACE / "folio_full.json", "folio"),
        (WORKSPACE / "proofwriter_owa_d5_full.json", "proofwriter"),
        (WORKSPACE / "ruletaker_d3d5_full.json", "ruletaker"),
        (WORKSPACE / "clutrr_full.json", "clutrr"),
    ]

    datasets = []
    for file_path, dataset_name in dataset_files:
        if not file_path.exists():
            logger.warning(f"File not found: {file_path}")
            continue
        entry = load_and_convert(file_path, dataset_name)
        if entry["examples"]:
            datasets.append(entry)

    output = {
        "metadata": {
            "description": "Neuro-symbolic reasoning benchmarks: FOLIO, ProofWriter, RuleTaker, CLUTRR",
            "source": "HuggingFace: tasksource/folio, tasksource/proofwriter, tasksource/ruletaker, kendrivp/CLUTRR_v1_extracted",
            "schema_version": "exp_sel_data_out_v1",
        },
        "datasets": datasets,
    }

    out_path = WORKSPACE / "full_data_out.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))

    total = sum(len(d["examples"]) for d in datasets)
    size_mb = out_path.stat().st_size / 1024 / 1024
    logger.info(f"Saved full_data_out.json: {len(datasets)} datasets, {total} examples, {size_mb:.1f}MB")

    for d in datasets:
        logger.info(f"  {d['dataset']}: {len(d['examples'])} examples")

    logger.info("Done")


if __name__ == "__main__":
    main()

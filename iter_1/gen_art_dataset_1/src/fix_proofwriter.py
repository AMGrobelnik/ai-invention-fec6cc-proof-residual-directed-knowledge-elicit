#!/usr/bin/env python3
"""Fix ProofWriter: stream with corrected OWA filter (id field contains 'OWA')."""

import json, sys, re, gc
from pathlib import Path
from loguru import logger
from datasets import load_dataset

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/fix_proofwriter.log", rotation="30 MB", level="DEBUG")

WORKSPACE = Path("/ai-inventor/aii_data/runs/b16e7/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")


def parse_proofwriter_theory(theory: str) -> list[dict]:
    predicates = []
    atoms = re.findall(r'\b([A-Z][a-z]+)\b.*?\bis\b.*?\b([a-z]+)\b', theory)
    for entity, prop in atoms[:20]:
        predicates.append({'name': prop, 'args': [entity], 'truth_value': True})
    return predicates[:10]


def proofwriter_to_schema(row: dict, split: str) -> dict:
    theory = row.get('theory', '') or ''
    premises = [p.strip() for p in theory.split('\n') if p.strip()]
    config = row.get('config', '')
    row_id = row.get('id', '')
    return {
        "id": f"proofwriter_{row_id}",
        "premises": premises,
        "hypothesis": row.get('question', ''),
        "label": str(row.get('answer', '')),
        "gold_predicates": parse_proofwriter_theory(theory),
        "dataset": "proofwriter",
        "depth": int(row.get('maxD', 0) or row.get('QDep', 0) or 0),
        "split": split,
        "metadata": {
            "NFact": row.get('NFact'),
            "NRule": row.get('NRule'),
            "QLen": row.get('QLen'),
            "config": config,
        }
    }


def main():
    rows = []
    max_per_split = 50000

    for split_name, hf_split in [('train', 'train'), ('test', 'test'), ('validation', 'dev')]:
        try:
            ds = load_dataset("tasksource/proofwriter", split=hf_split, streaming=True)
            count = 0
            for row in ds:
                row_id = row.get('id', '')
                max_d = row.get('maxD', 0) or 0
                # OWA is in the id field; depth >= 5
                if 'OWA' in row_id and int(max_d) >= 5:
                    rows.append(proofwriter_to_schema(row, split_name))
                    count += 1
                    if count % 5000 == 0:
                        logger.info(f"  ProofWriter {split_name}: {count} rows filtered...")
                    if count >= max_per_split:
                        logger.info(f"  Capped {split_name} at {max_per_split}")
                        break
            logger.info(f"ProofWriter {split_name}: {count} OWA+D5 rows")
        except Exception as e:
            logger.warning(f"ProofWriter {split_name} ({hf_split}): {e}")

    logger.info(f"ProofWriter total: {len(rows)} rows")

    pw_path = WORKSPACE / "proofwriter_owa_d5_full.json"
    pw_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    logger.info(f"Saved proofwriter_owa_d5_full.json ({pw_path.stat().st_size // 1024 // 1024}MB)")

    # Update data_out.json to include ProofWriter rows
    if rows:
        data_out_path = WORKSPACE / "data_out.json"
        combined = json.loads(data_out_path.read_text())
        # Remove any existing proofwriter rows (there are none from previous run)
        import random; random.seed(42)
        pw_sample = rows if len(rows) <= 10000 else random.sample(rows, 10000)
        combined.extend(pw_sample)
        data_out_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
        logger.info(f"Updated data_out.json: {len(combined)} rows ({data_out_path.stat().st_size / 1024 / 1024:.1f}MB)")

    logger.info("Done")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Process and standardize neuro-symbolic reasoning datasets: FOLIO, ProofWriter, RuleTaker, CLUTRR."""

import json
import re
import sys
import ast
import random
import gc
from pathlib import Path
from typing import Optional

from loguru import logger
from datasets import load_dataset

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/process.log", rotation="30 MB", level="DEBUG")

WORKSPACE = Path("/ai-inventor/aii_data/runs/b16e7/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")
TEMP_DIR = WORKSPACE / "temp" / "datasets"
RANDOM_SEED = 42
random.seed(RANDOM_SEED)


# ---------- Gold predicate parsers ----------

def parse_folio_fol(fol_string: str) -> list[dict]:
    """Extract predicate atoms from FOLIO FOL string."""
    atoms = re.findall(r'([A-Z][A-Za-z0-9]*)\(([^)]+)\)', fol_string)
    predicates = []
    for name, args_str in atoms:
        args = [a.strip() for a in args_str.split(',')]
        predicates.append({'name': name, 'args': args, 'truth_value': True})
    return predicates


def parse_proofwriter_theory(theory: str) -> list[dict]:
    """Extract ground atoms from ProofWriter theory (NL facts/rules)."""
    # Extract 'X is Y' or 'X is Y of Z' patterns with capitalized names
    predicates = []
    atoms = re.findall(r'\b([A-Z][a-z]+)\b.*?\bis\b.*?\b([a-z]+)\b', theory)
    for entity, prop in atoms[:20]:
        predicates.append({'name': prop, 'args': [entity], 'truth_value': True})
    return predicates[:10]  # cap at 10


def parse_clutrr_predicates(row: dict) -> list[dict]:
    """Extract gold relational predicates from CLUTRR story_edges + edge_types."""
    predicates = []
    try:
        edge_types_raw = row.get('edge_types', '[]')
        edge_types = ast.literal_eval(edge_types_raw) if isinstance(edge_types_raw, str) else edge_types_raw

        # Extract named entities from story
        story = row.get('story', '')
        entities = re.findall(r'\[([A-Z][a-z]+)\]', story)
        unique_entities = list(dict.fromkeys(entities))

        story_edges_raw = row.get('story_edges', '[]')
        story_edges = ast.literal_eval(story_edges_raw) if isinstance(story_edges_raw, str) else story_edges_raw

        for (i, j), rel in zip(story_edges, edge_types):
            e1 = unique_entities[i] if i < len(unique_entities) else f"entity_{i}"
            e2 = unique_entities[j] if j < len(unique_entities) else f"entity_{j}"
            predicates.append({'name': rel, 'args': [e1, e2], 'truth_value': True})

        # Add query target
        query_raw = row.get('query', '()')
        target_text = row.get('target_text', '')
        if target_text:
            try:
                q = ast.literal_eval(query_raw) if isinstance(query_raw, str) else query_raw
                if isinstance(q, (tuple, list)) and len(q) == 2:
                    predicates.append({'name': target_text, 'args': list(q), 'truth_value': True})
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"CLUTRR predicate parse error: {e}")
    return predicates


# ---------- Schema conversion functions ----------

def folio_to_schema(row: dict, split: str) -> dict:
    fol = row.get('premises-FOL', '') or ''
    conclusion_fol = row.get('conclusion-FOL', '') or ''
    gold_preds = parse_folio_fol(fol + '\n' + conclusion_fol)
    premises_text = row.get('premises', '')
    premises = [p.strip() for p in premises_text.split('\n') if p.strip()]
    return {
        "id": f"folio_{row.get('example_id', row.get('story_id', 'unk'))}",
        "premises": premises,
        "hypothesis": row.get('conclusion', ''),
        "label": str(row.get('label', '')),
        "gold_predicates": gold_preds,
        "dataset": "folio",
        "depth": 3,
        "split": split,
        "metadata": {
            "story_id": row.get('story_id'),
            "premises_fol": (fol[:500] if fol else ''),
            "conclusion_fol": (conclusion_fol[:200] if conclusion_fol else ''),
        }
    }


def proofwriter_to_schema(row: dict, split: str) -> dict:
    theory = row.get('theory', '') or ''
    premises = [p.strip() for p in theory.split('\n') if p.strip()]
    gold_preds = parse_proofwriter_theory(theory)
    config = row.get('config', '')
    return {
        "id": f"proofwriter_{row.get('id', 'unk')}",
        "premises": premises,
        "hypothesis": row.get('question', ''),
        "label": str(row.get('answer', '')),
        "gold_predicates": gold_preds,
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


def ruletaker_to_schema(row: dict, split: str) -> dict:
    context = row.get('context', '') or ''
    premises = [p.strip() for p in re.split(r'\.\s+', context) if p.strip()]
    config = row.get('config', '')
    depth_match = re.search(r'depth-(\d+)', config)
    depth = int(depth_match.group(1)) if depth_match else 0
    return {
        "id": f"ruletaker_{abs(hash(context[:50] + row.get('question', '')))}",
        "premises": premises,
        "hypothesis": row.get('question', ''),
        "label": row.get('label', ''),
        "gold_predicates": [],
        "dataset": "ruletaker",
        "depth": depth,
        "split": split,
        "metadata": {"config": config}
    }


def clutrr_to_schema(row: dict, split: str) -> dict:
    story = row.get('story', '') or ''
    premises = [p.strip() + '.' for p in story.replace('\n', ' ').split('.') if p.strip()]
    query_raw = row.get('query', '()')
    target_text = row.get('target_text', '') or row.get('answer', '')
    try:
        q = ast.literal_eval(query_raw) if isinstance(query_raw, str) else query_raw
        hypothesis = f"{q[0]} is {target_text} of {q[1]}" if isinstance(q, (tuple, list)) and len(q) == 2 else story[:100]
    except Exception:
        hypothesis = query_raw

    # Parse depth from task_name e.g. 'task_1.2' -> 2
    task_name = row.get('task_name', '')
    depth_match = re.search(r'task_\d+\.(\d+)', task_name)
    depth = int(depth_match.group(1)) if depth_match else 0

    return {
        "id": f"clutrr_{row.get('id', 'unk')}",
        "premises": premises,
        "hypothesis": hypothesis,
        "label": "True",
        "gold_predicates": parse_clutrr_predicates(row),
        "dataset": "clutrr",
        "depth": depth,
        "split": split,
        "metadata": {
            "task_name": task_name,
            "f_comb": row.get('f_comb', ''),
            "clean_story": (row.get('clean_story', '')[:300] if row.get('clean_story') else ''),
        }
    }


# ---------- Load and process each dataset ----------

@logger.catch(reraise=True)
def process_folio() -> list[dict]:
    logger.info("Processing FOLIO...")
    rows = []
    for split in ['train', 'validation']:
        path = TEMP_DIR / f"full_tasksource_folio_default_{split}.json"
        data = json.loads(path.read_text())
        for row in data:
            rows.append(folio_to_schema(row, split))
    logger.info(f"FOLIO: {len(rows)} rows")
    return rows


@logger.catch(reraise=True)
def process_proofwriter(max_rows: int = 50000) -> list[dict]:
    logger.info("Processing ProofWriter (streaming from HF, OWA + maxD>=5)...")
    rows = []
    try:
        ds = load_dataset("tasksource/proofwriter", split="train", streaming=True)
        count = 0
        for row in ds:
            row_id = row.get('id', '')
            max_d = row.get('maxD', 0) or 0
            # Filter: OWA (in id) and depth >= 5
            if 'OWA' in row_id and int(max_d) >= 5:
                for split_name in ['train']:
                    rows.append(proofwriter_to_schema(row, 'train'))
                count += 1
                if count % 5000 == 0:
                    logger.info(f"  ProofWriter filtered: {count} rows so far...")
                if count >= max_rows:
                    logger.info(f"  Capped at {max_rows} rows")
                    break
    except Exception as e:
        logger.error(f"ProofWriter train streaming failed: {e}")

    # Also get validation/test splits
    for split_name in ['validation', 'test']:
        try:
            ds = load_dataset("tasksource/proofwriter", split=split_name, streaming=True)
            split_count = 0
            for row in ds:
                config = row.get('config', '')
                max_d = row.get('maxD', 0) or 0
                if 'OWA' in config and int(max_d) >= 5:
                    rows.append(proofwriter_to_schema(row, split_name))
                    split_count += 1
                    if split_count >= 5000:
                        break
        except Exception as e:
            logger.warning(f"ProofWriter {split_name} failed: {e}")

    logger.info(f"ProofWriter: {len(rows)} rows (OWA, maxD>=5)")
    return rows


@logger.catch(reraise=True)
def process_ruletaker(max_rows: int = 50000) -> list[dict]:
    logger.info("Processing RuleTaker (streaming from HF, depth-3 and depth-5)...")
    rows = []
    try:
        ds = load_dataset("tasksource/ruletaker", split="train", streaming=True)
        count = 0
        for row in ds:
            config = row.get('config', '')
            if 'depth-3' in config or 'depth-5' in config:
                rows.append(ruletaker_to_schema(row, 'train'))
                count += 1
                if count % 10000 == 0:
                    logger.info(f"  RuleTaker filtered: {count} rows so far...")
                if count >= max_rows:
                    logger.info(f"  Capped at {max_rows} rows")
                    break
    except Exception as e:
        logger.error(f"RuleTaker train streaming failed: {e}")

    for split_name in ['validation', 'test']:
        try:
            ds = load_dataset("tasksource/ruletaker", split=split_name, streaming=True)
            split_count = 0
            for row in ds:
                config = row.get('config', '')
                if 'depth-3' in config or 'depth-5' in config:
                    rows.append(ruletaker_to_schema(row, split_name))
                    split_count += 1
                    if split_count >= 10000:
                        break
        except Exception as e:
            logger.warning(f"RuleTaker {split_name} failed: {e}")

    logger.info(f"RuleTaker: {len(rows)} rows (depth-3 + depth-5)")
    return rows


@logger.catch(reraise=True)
def process_clutrr(max_rows_per_split: int = 15000) -> list[dict]:
    logger.info("Processing CLUTRR...")
    rows = []
    # The kendrivp/CLUTRR_v1_extracted has both train and test splits
    # task_split field already encodes the original split
    for split_name in ['train', 'test']:
        path = TEMP_DIR / f"full_kendrivp_CLUTRR_v1_extracted_default_{split_name}.json"
        if not path.exists():
            logger.warning(f"CLUTRR {split_name} file not found: {path}")
            continue
        data = json.loads(path.read_text())
        count = 0
        for row in data:
            rows.append(clutrr_to_schema(row, split_name))
            count += 1
            if count >= max_rows_per_split:
                logger.info(f"  CLUTRR {split_name}: capped at {max_rows_per_split}")
                break
    logger.info(f"CLUTRR: {len(rows)} rows")
    return rows


# ---------- Pilot split creation ----------

def create_pilot_splits(
    ruletaker_rows: list[dict],
    clutrr_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Create 200-row pilot split and 200-row held-out split."""
    logger.info("Creating pilot splits...")
    random.seed(RANDOM_SEED)

    # RuleTaker pilot: 50 entailment + 50 not_entailment from test split, depth-3
    rt_test_d3 = [r for r in ruletaker_rows if r['split'] == 'test' and r['depth'] == 3]
    rt_entailment = [r for r in rt_test_d3 if r['label'] == 'entailment']
    rt_not = [r for r in rt_test_d3 if r['label'] == 'not entailment']

    if len(rt_entailment) < 100 or len(rt_not) < 100:
        # Fall back to depth-5
        rt_test_all = [r for r in ruletaker_rows if r['split'] == 'test']
        rt_entailment = [r for r in rt_test_all if r['label'] == 'entailment']
        rt_not = [r for r in rt_test_all if r['label'] == 'not entailment']

    rt_ent_sample = random.sample(rt_entailment, min(100, len(rt_entailment)))
    rt_not_sample = random.sample(rt_not, min(100, len(rt_not)))

    rt_pilot_50 = rt_ent_sample[:50] + rt_not_sample[:50]
    rt_held_50 = rt_ent_sample[50:100] + rt_not_sample[50:100]

    # Mark as pilot/held_out
    for r in rt_pilot_50:
        r = dict(r); r['split'] = 'pilot'
    for r in rt_held_50:
        r = dict(r); r['split'] = 'held_out'

    pilot_rt = [{**r, 'split': 'pilot'} for r in rt_pilot_50]
    held_rt = [{**r, 'split': 'held_out'} for r in rt_held_50]

    # CLUTRR pilot: 100 from test split
    clutrr_test = [r for r in clutrr_rows if r['split'] == 'test']
    if len(clutrr_test) < 200:
        clutrr_test = clutrr_rows  # fallback
    clutrr_pilot_200 = random.sample(clutrr_test, min(200, len(clutrr_test)))
    clutrr_pilot = [{**r, 'split': 'pilot'} for r in clutrr_pilot_200[:100]]
    clutrr_held = [{**r, 'split': 'held_out'} for r in clutrr_pilot_200[100:200]]

    pilot_split = pilot_rt + clutrr_pilot
    held_out_split = held_rt + clutrr_held

    random.shuffle(pilot_split)
    random.shuffle(held_out_split)

    logger.info(f"Pilot split: {len(pilot_split)} rows, Held-out: {len(held_out_split)} rows")
    return pilot_split, held_out_split


# ---------- Statistics ----------

def compute_stats(rows: list[dict], name: str) -> dict:
    """Compute dataset statistics."""
    label_counts: dict[str, int] = {}
    depths = []
    gold_pred_counts = []
    entity_counts = []

    for r in rows:
        lb = r.get('label', 'unknown')
        label_counts[lb] = label_counts.get(lb, 0) + 1
        depths.append(r.get('depth', 0))
        gp = r.get('gold_predicates', [])
        gold_pred_counts.append(len(gp))
        # Count unique capitalized tokens in premises as proxy for entities
        all_text = ' '.join(r.get('premises', []))
        entities = set(re.findall(r'\b[A-Z][a-z]+\b', all_text))
        entity_counts.append(len(entities))

    total = len(rows)
    stats = {
        "dataset": name,
        "total_rows": total,
        "label_distribution": {k: {"count": v, "pct": round(v / total * 100, 1)} for k, v in label_counts.items()},
        "mean_depth": round(sum(depths) / max(1, len(depths)), 2),
        "max_depth": max(depths) if depths else 0,
        "fraction_with_gold_predicates": round(sum(1 for c in gold_pred_counts if c > 0) / max(1, total), 3),
        "mean_gold_predicate_count": round(sum(gold_pred_counts) / max(1, total), 2),
        "mean_entity_count": round(sum(entity_counts) / max(1, total), 2),
    }
    return stats


# ---------- Stratified sampling ----------

def stratified_sample(rows: list[dict], n: int, key: str = 'label') -> list[dict]:
    """Stratified sample by key to get n rows."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r.get(key, 'unknown')].append(r)
    result = []
    per_class = max(1, n // len(groups))
    for cls_rows in groups.values():
        sample = random.sample(cls_rows, min(per_class, len(cls_rows)))
        result.extend(sample)
    # Fill up to n
    remaining = [r for r in rows if r not in set(id(x) for x in result)]
    random.shuffle(rows)
    while len(result) < n and rows:
        r = rows.pop()
        if r not in result:
            result.append(r)
    return result[:n]


@logger.catch(reraise=True)
def main():
    out_dir = WORKSPACE
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=== Starting dataset processing ===")

    # 1. FOLIO
    folio_rows = process_folio()
    folio_path = WORKSPACE / "folio_full.json"
    folio_path.write_text(json.dumps(folio_rows, indent=2, ensure_ascii=False))
    logger.info(f"Saved folio_full.json: {len(folio_rows)} rows ({folio_path.stat().st_size // 1024}KB)")

    # 2. ProofWriter (streaming)
    proofwriter_rows = process_proofwriter(max_rows=50000)
    pw_path = WORKSPACE / "proofwriter_owa_d5_full.json"
    pw_path.write_text(json.dumps(proofwriter_rows, indent=2, ensure_ascii=False))
    logger.info(f"Saved proofwriter_owa_d5_full.json: {len(proofwriter_rows)} rows ({pw_path.stat().st_size // 1024 // 1024}MB)")
    del proofwriter_rows
    gc.collect()
    proofwriter_rows = json.loads(pw_path.read_text())

    # 3. RuleTaker (streaming)
    ruletaker_rows = process_ruletaker(max_rows=50000)
    rt_path = WORKSPACE / "ruletaker_d3d5_full.json"
    rt_path.write_text(json.dumps(ruletaker_rows, indent=2, ensure_ascii=False))
    logger.info(f"Saved ruletaker_d3d5_full.json: {len(ruletaker_rows)} rows ({rt_path.stat().st_size // 1024 // 1024}MB)")

    # 4. CLUTRR
    clutrr_rows = process_clutrr(max_rows_per_split=15000)
    cl_path = WORKSPACE / "clutrr_full.json"
    cl_path.write_text(json.dumps(clutrr_rows, indent=2, ensure_ascii=False))
    logger.info(f"Saved clutrr_full.json: {len(clutrr_rows)} rows ({cl_path.stat().st_size // 1024 // 1024}MB)")

    # 5. Pilot splits
    pilot_split, held_out_split = create_pilot_splits(ruletaker_rows, clutrr_rows)
    (WORKSPACE / "pilot_split.json").write_text(json.dumps(pilot_split, indent=2, ensure_ascii=False))
    (WORKSPACE / "pilot_held_out.json").write_text(json.dumps(held_out_split, indent=2, ensure_ascii=False))
    logger.info(f"Pilot: {len(pilot_split)}, Held-out: {len(held_out_split)}")

    # 6. Combined data_out.json (unified)
    logger.info("Building unified data_out.json...")
    combined = []
    combined.extend(folio_rows)  # all ~1203

    # ProofWriter: max 10k stratified
    pw_rows = json.loads(pw_path.read_text())
    if len(pw_rows) > 10000:
        random.seed(RANDOM_SEED)
        random.shuffle(pw_rows)
        pw_rows = pw_rows[:10000]
    combined.extend(pw_rows)
    del pw_rows
    gc.collect()

    # RuleTaker: max 10k stratified
    if len(ruletaker_rows) > 10000:
        random.seed(RANDOM_SEED)
        random.shuffle(ruletaker_rows)
        rt_sample = ruletaker_rows[:10000]
    else:
        rt_sample = ruletaker_rows
    combined.extend(rt_sample)
    del ruletaker_rows
    gc.collect()

    # CLUTRR: all ~30k (capped at 15k per split * 2)
    combined.extend(clutrr_rows)
    del clutrr_rows
    gc.collect()

    # Add pilot/held_out rows (tagged)
    combined.extend(pilot_split)
    combined.extend(held_out_split)

    logger.info(f"Combined total: {len(combined)} rows")
    data_out_path = WORKSPACE / "data_out.json"
    data_out_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    data_size_mb = data_out_path.stat().st_size / 1024 / 1024
    logger.info(f"Saved data_out.json: {len(combined)} rows ({data_size_mb:.1f}MB)")

    # 7. Dataset statistics
    folio_data = json.loads((WORKSPACE / "folio_full.json").read_text())
    pw_all = json.loads(pw_path.read_text())
    rt_all = json.loads(rt_path.read_text())
    cl_all = json.loads(cl_path.read_text())

    stats = {
        "folio": compute_stats(folio_data, "folio"),
        "proofwriter": compute_stats(pw_all, "proofwriter"),
        "ruletaker": compute_stats(rt_all, "ruletaker"),
        "clutrr": compute_stats(cl_all, "clutrr"),
        "pilot_split": compute_stats(pilot_split, "pilot"),
        "held_out": compute_stats(held_out_split, "held_out"),
        "combined": compute_stats(combined, "combined"),
    }

    stats_path = WORKSPACE / "dataset_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2))
    logger.info(f"Saved dataset_stats.json")

    # Print summary
    for ds_name, ds_stats in stats.items():
        logger.info(f"  {ds_name}: {ds_stats['total_rows']} rows, labels={list(ds_stats['label_distribution'].keys())}")

    logger.info("=== Processing complete ===")


if __name__ == "__main__":
    main()

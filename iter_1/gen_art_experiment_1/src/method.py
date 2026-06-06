#!/usr/bin/env python3
"""Proof-Residual Directed Knowledge Elicitation Pipeline.

Full 5-stage pipeline: pilot comparison, schema coverage audit,
residual-mode meta-interpreter, binary elicitation, proof execution
with provenance-annotated evaluation vs CoT and Logic-LM baselines.
"""

import asyncio
import gc
import json
import math
import os
import re
import resource
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from scipy.stats import spearmanr

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

# ── Resource limits ──────────────────────────────────────────────────────────
_avail_bytes = int(Path("/sys/fs/cgroup/memory.max").read_text().strip())
RAM_BUDGET = min(int(_avail_bytes * 0.80), 22 * 1024**3)
resource.setrlimit(resource.RLIMIT_AS, (RAM_BUDGET * 3, RAM_BUDGET * 3))
logger.info(f"RAM budget: {RAM_BUDGET/1e9:.1f} GB")

# ── OpenRouter config ────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
PRIMARY_MODEL = "meta-llama/llama-3.1-8b-instruct"
FALLBACK_MODEL = "mistralai/mistral-7b-instruct-v0.3"

WORKSPACE = Path("/ai-inventor/aii_data/runs/b16e7/3_invention_loop/iter_1/gen_art/gen_art_experiment_1")

# ── Seed Schema ──────────────────────────────────────────────────────────────
SEED_SCHEMA = {
    "parent": {"arity": 2, "types": ("Person", "Person"), "nl": "{0} is a parent of {1}"},
    "sibling": {"arity": 2, "types": ("Person", "Person"), "nl": "{0} is a sibling of {1}"},
    "employs": {"arity": 2, "types": ("Organization", "Person"), "nl": "{0} employs {1}"},
    "knows": {"arity": 2, "types": ("Person", "Person"), "nl": "{0} knows {1}"},
    "married_to": {"arity": 2, "types": ("Person", "Person"), "nl": "{0} is married to {1}"},
    "lives_in": {"arity": 2, "types": ("Person", "Location"), "nl": "{0} lives in {1}"},
    "friend_of": {"arity": 2, "types": ("Person", "Person"), "nl": "{0} is a friend of {1}"},
    "manages": {"arity": 2, "types": ("Person", "Person"), "nl": "{0} manages {1}"},
    "causes": {"arity": 2, "types": ("Event", "Event"), "nl": "{0} causes {1}"},
    "enables": {"arity": 2, "types": ("Event", "Event"), "nl": "{0} enables {1}"},
    "prevents": {"arity": 2, "types": ("Event", "Event"), "nl": "{0} prevents {1}"},
    "results_in": {"arity": 2, "types": ("Event", "Outcome"), "nl": "{0} results in {1}"},
    "occurred_before": {"arity": 2, "types": ("Event", "Event"), "nl": "{0} occurred before {1}"},
    "during": {"arity": 2, "types": ("Event", "Event"), "nl": "{0} happened during {1}"},
    "starts_at": {"arity": 2, "types": ("Event", "Time"), "nl": "{0} starts at {1}"},
    "ends_at": {"arity": 2, "types": ("Event", "Time"), "nl": "{0} ends at {1}"},
    "simultaneous": {"arity": 2, "types": ("Event", "Event"), "nl": "{0} and {1} are simultaneous"},
    "obligated": {"arity": 2, "types": ("Agent", "Action"), "nl": "{0} is obligated to {1}"},
    "permitted": {"arity": 2, "types": ("Agent", "Action"), "nl": "{0} is permitted to {1}"},
    "prohibited": {"arity": 2, "types": ("Agent", "Action"), "nl": "{0} is prohibited from {1}"},
    "required_for": {"arity": 2, "types": ("Condition", "Goal"), "nl": "{0} is required for {1}"},
    "is_a": {"arity": 2, "types": ("Entity", "Category"), "nl": "{0} is a {1}"},
    "has_property": {"arity": 2, "types": ("Entity", "Property"), "nl": "{0} has property {1}"},
    "located_in": {"arity": 2, "types": ("Entity", "Location"), "nl": "{0} is located in {1}"},
    "part_of": {"arity": 2, "types": ("Entity", "Entity"), "nl": "{0} is part of {1}"},
    "owns": {"arity": 2, "types": ("Agent", "Entity"), "nl": "{0} owns {1}"},
    "member_of": {"arity": 2, "types": ("Person", "Group"), "nl": "{0} is a member of {1}"},
}
P = len(SEED_SCHEMA)

# ── Cost Tracker ─────────────────────────────────────────────────────────────
class BudgetExceeded(Exception):
    pass


class CostTracker:
    def __init__(self, hard_limit: float = 8.0):
        self.cumulative_usd = 0.0
        self.hard_limit = hard_limit
        self.n_calls = 0

    def add(self, input_tokens: int, output_tokens: int, model: str = PRIMARY_MODEL):
        if "llama-3.1-8b" in model or "llama-3.2" in model:
            rate_in = 0.06 / 1_000_000
            rate_out = 0.06 / 1_000_000
        elif "mistral-7b" in model:
            rate_in = 0.055 / 1_000_000
            rate_out = 0.055 / 1_000_000
        else:
            rate_in = 0.15 / 1_000_000
            rate_out = 0.15 / 1_000_000
        cost = input_tokens * rate_in + output_tokens * rate_out
        self.cumulative_usd += cost
        self.n_calls += 1
        if self.n_calls % 50 == 0:
            logger.info(f"Cost: ${self.cumulative_usd:.4f} after {self.n_calls} calls")
        if self.cumulative_usd > self.hard_limit:
            raise BudgetExceeded(f"Spent ${self.cumulative_usd:.2f}, stopping")

    def check(self, margin: float = 0.5):
        if self.cumulative_usd + margin > self.hard_limit:
            raise BudgetExceeded(f"Near limit: ${self.cumulative_usd:.2f}")


cost_tracker = CostTracker()

# ── LLM Client ───────────────────────────────────────────────────────────────
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

async def _call_single(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    prompt: str,
    model: str,
    system: str = "",
    max_tokens: int = 200,
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://ai-inventor.research",
    }

    for attempt in range(4):
        async with sem:
            try:
                r = await client.post(BASE_URL, json=payload, headers=headers, timeout=60.0)
                if r.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.debug(f"Rate limit, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                data = r.json()
                usage = data.get("usage", {})
                cost_tracker.add(
                    usage.get("prompt_tokens", len(prompt) // 4),
                    usage.get("completion_tokens", 50),
                    model,
                )
                return data["choices"][0]["message"]["content"].strip()
            except BudgetExceeded:
                raise
            except httpx.TimeoutException:
                logger.warning(f"Timeout on attempt {attempt+1}")
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.warning(f"LLM call error attempt {attempt+1}: {e}")
                await asyncio.sleep(2 ** attempt)
    return "UNCERTAIN"


async def call_llm_batch(
    prompts: list[str],
    model: str = PRIMARY_MODEL,
    system: str = "",
    max_tokens: int = 200,
    max_concurrent: int = 15,
) -> list[str]:
    sem = asyncio.Semaphore(max_concurrent)
    async with httpx.AsyncClient() as client:
        tasks = [
            _call_single(client, sem, p, model, system, max_tokens)
            for p in prompts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, BudgetExceeded):
            raise r
        if isinstance(r, Exception):
            logger.warning(f"Batch item error: {r}")
            out.append("UNCERTAIN")
        else:
            out.append(r)
    return out


# ── Prompt Templates ──────────────────────────────────────────────────────────
BINARY_PROMPT = """\
Document:
{premises}

Statement: {predicate_nl}

Answer with EXACTLY one token from: YES, NO, or UNCERTAIN
Then optionally add: [cite: "exact text from document"]

Your answer:"""

OPEN_FOL_PROMPT = """\
Premises:
{premises}

Hypothesis: {hypothesis}

List all relevant atomic predicates (format: predicate_name(arg1, arg2) = TRUE/FALSE):"""

COT_PROMPT = """\
Premises:
{premises}

Hypothesis: {hypothesis}

Think step by step, then answer True, False, or Uncertain on the last line."""

LOGIC_LM_PROMPT = """\
Premises:
{premises}

Hypothesis: {hypothesis}

Step 1: Write FOL for each premise.
Step 2: Write FOL for hypothesis.
Step 3: Determine if hypothesis follows logically.
Answer: True / False / Uncertain"""

SYSTEM_BINARY = "You are a precise fact-checker. Answer with YES, NO, or UNCERTAIN only."
SYSTEM_REASONING = "You are a logical reasoning assistant. Be precise and concise."


# ── Response Parsers ──────────────────────────────────────────────────────────
def parse_binary_response(response: str, premises: str) -> dict:
    resp = response.strip()
    truth = "UNCERTAIN"
    span = None

    m = re.search(r'\b(YES|NO|UNCERTAIN)\b', resp, re.IGNORECASE)
    if m:
        truth = m.group(1).upper()

    cite_m = re.search(r'\[cite:\s*"([^"]+)"\]', resp)
    if cite_m:
        span = cite_m.group(1)

    grounded = False
    if span and span.strip() in premises:
        grounded = True
    elif span and len(span) > 5 and any(
        word in premises.lower() for word in span.lower().split()[:3] if len(word) > 3
    ):
        grounded = True

    return {"truth_value": truth, "span": span, "grounded": grounded}


def parse_fol_response(response: str) -> list[dict]:
    results = []
    pattern = re.compile(
        r'([A-Za-z_][A-Za-z0-9_]*)\(([^)]+)\)\s*=\s*(TRUE|FALSE|true|false)',
        re.IGNORECASE,
    )
    for m in pattern.finditer(response):
        pred = m.group(1).lower()
        args = tuple(a.strip().lower() for a in m.group(2).split(","))
        truth = m.group(3).upper()
        results.append({"predicate": pred, "args": args, "truth_value": truth})
    return results


def parse_answer(response: str) -> str:
    """Parse True/False/Uncertain from reasoning response."""
    resp = response.strip()
    # Look for final answer pattern
    for pattern in [
        r'(?:Answer|answer|ANSWER):\s*(True|False|Uncertain)',
        r'\b(True|False|Uncertain)\b(?:\s*$)',
        r'(?i)\b(true|false|uncertain)\b',
    ]:
        m = re.search(pattern, resp)
        if m:
            val = m.group(1).capitalize()
            if val in ("True", "False", "Uncertain"):
                return val
    return "Uncertain"


# ── FOL Parsing ───────────────────────────────────────────────────────────────
def extract_predicate_atoms(fol_str: str) -> list[tuple[str, tuple]]:
    """Extract (predicate, args_tuple) from FOL string."""
    pattern = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\(([^)]+)\)')
    results = []
    for m in pattern.finditer(fol_str):
        pred = m.group(1)
        args = tuple(a.strip() for a in m.group(2).split(","))
        # Skip logical connectives used as predicates
        if pred.lower() not in {"forall", "exists", "not", "and", "or", "implies"}:
            results.append((pred.lower(), args))
    return results


def normalize_args(args: tuple) -> tuple:
    """Lowercase and clean argument names."""
    return tuple(a.lower().strip().replace(" ", "_") for a in args)


# ── Entity Extraction ─────────────────────────────────────────────────────────
_nlp = None

def get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


SPACY_LABEL_MAP = {
    "PERSON": "Person", "ORG": "Organization", "GPE": "Location",
    "LOC": "Location", "EVENT": "Event", "WORK_OF_ART": "Entity",
    "PRODUCT": "Entity", "FAC": "Location",
}


def extract_entities(text: str) -> dict[str, str]:
    """Extract entities: {entity_text_lower: type}."""
    nlp = get_nlp()
    doc = nlp(text)
    entities = {}
    for ent in doc.ents:
        label = SPACY_LABEL_MAP.get(ent.label_, "Entity")
        entities[ent.text.lower()] = label
    # Also capture capitalized tokens not caught by NER
    for token in doc:
        if token.is_alpha and token.text[0].isupper() and not token.is_stop:
            key = token.text.lower()
            if key not in entities:
                entities[key] = "Entity"
    return entities


# ── Initial KB Builder ────────────────────────────────────────────────────────
PATTERN_RULES = [
    (re.compile(r'(\w+) is a parent of (\w+)', re.I), "parent", (0, 1)),
    (re.compile(r'(\w+) and (\w+) are siblings', re.I), "sibling", (0, 1)),
    (re.compile(r'(\w+) is a sibling of (\w+)', re.I), "sibling", (0, 1)),
    (re.compile(r'(\w+) works (?:at|for) (\w+)', re.I), "employs", (1, 0)),
    (re.compile(r'(\w+) is married to (\w+)', re.I), "married_to", (0, 1)),
    (re.compile(r'(\w+) lives in (\w+)', re.I), "lives_in", (0, 1)),
    (re.compile(r'(\w+) is a member of (\w+)', re.I), "member_of", (0, 1)),
    (re.compile(r'(\w+) is a (\w+)', re.I), "is_a", (0, 1)),
    (re.compile(r'(\w+) owns (\w+)', re.I), "owns", (0, 1)),
    (re.compile(r'(\w+) manages (\w+)', re.I), "manages", (0, 1)),
    (re.compile(r'(\w+) knows (\w+)', re.I), "knows", (0, 1)),
    (re.compile(r'(\w+) is located in (\w+)', re.I), "located_in", (0, 1)),
    (re.compile(r'(\w+) is part of (\w+)', re.I), "part_of", (0, 1)),
    (re.compile(r'(\w+) is a friend of (\w+)', re.I), "friend_of", (0, 1)),
    (re.compile(r'(\w+) causes (\w+)', re.I), "causes", (0, 1)),
    (re.compile(r'(\w+) prevents (\w+)', re.I), "prevents", (0, 1)),
    (re.compile(r'(\w+) enables (\w+)', re.I), "enables", (0, 1)),
    (re.compile(r'(\w+) has (?:the )?property (\w+)', re.I), "has_property", (0, 1)),
]


def build_initial_kb(premises_text: str, entities: dict) -> dict:
    """Build KB with TEXT-STATED provenance from pattern matching."""
    kb: dict[str, dict] = {}
    for pattern, pred, (idx_a, idx_b) in PATTERN_RULES:
        for m in pattern.finditer(premises_text):
            groups = m.groups()
            if len(groups) >= 2:
                a = groups[idx_a].lower()
                b = groups[idx_b].lower()
                if pred not in kb:
                    kb[pred] = {}
                kb[pred][(a, b)] = {"provenance": "TEXT-STATED", "confidence": 1.0, "span": m.group(0)}
    return kb


# ── Schema Coverage Audit ─────────────────────────────────────────────────────
import difflib


def fuzzy_match_predicate(pred_name: str, schema: dict) -> str | None:
    """Try exact then fuzzy match against schema keys."""
    if pred_name in schema:
        return pred_name
    best = None
    best_score = 0.0
    for key in schema:
        score = difflib.SequenceMatcher(None, pred_name, key).ratio()
        if score > best_score:
            best_score = score
            best = key
    if best_score > 0.75:
        return best
    return None


def schema_coverage_audit(examples: list[dict], schema: dict) -> dict:
    covered_total = 0
    uncovered_total = 0
    uncovered_predicates: dict[str, int] = {}
    n_examples = 0

    for ex in examples:
        fol_fields = []
        if ex.get("premises-FOL"):
            fol_fields.append(ex["premises-FOL"])
        if ex.get("conclusion-FOL"):
            fol_fields.append(ex["conclusion-FOL"])

        if not fol_fields:
            continue

        n_examples += 1
        gold_preds = set()
        for fol in fol_fields:
            for pred, _ in extract_predicate_atoms(fol):
                gold_preds.add(pred)

        for pred in gold_preds:
            if fuzzy_match_predicate(pred, schema):
                covered_total += 1
            else:
                uncovered_total += 1
                uncovered_predicates[pred] = uncovered_predicates.get(pred, 0) + 1

    total = covered_total + uncovered_total
    mean_cov = covered_total / total if total > 0 else 0.0
    top20_uncovered = sorted(uncovered_predicates.items(), key=lambda x: -x[1])[:20]

    return {
        "mean_coverage": float(mean_cov),
        "covered": covered_total,
        "uncovered": uncovered_total,
        "n_examples": n_examples,
        "uncovered_predicates_top20": top20_uncovered,
    }


# ── Backward Chaining Meta-Interpreter ───────────────────────────────────────
@dataclass
class ProofResidual:
    predicate: str
    args: tuple
    type_constraints: tuple
    depth: int
    proof_context: list = field(default_factory=list)


@dataclass
class Rule:
    head_pred: str
    head_args: tuple
    body: list  # list of (pred, args)


# Built-in inference rules for common relations
BUILTIN_RULES = [
    # grandparent
    Rule("grandparent", ("X", "Z"), [("parent", ("X", "Y")), ("parent", ("Y", "Z"))]),
    # sibling (symmetric)
    Rule("sibling", ("X", "Y"), [("sibling", ("Y", "X"))]),
    # uncle/aunt via parent-sibling
    Rule("uncle_of", ("X", "Y"), [("sibling", ("X", "P")), ("parent", ("P", "Y"))]),
    # transitive located_in
    Rule("located_in", ("X", "Z"), [("located_in", ("X", "Y")), ("located_in", ("Y", "Z"))]),
    # part_of transitive
    Rule("part_of", ("X", "Z"), [("part_of", ("X", "Y")), ("part_of", ("Y", "Z"))]),
]


class BackwardChainingInterpreter:
    def __init__(self, kb: dict, schema: dict, max_depth: int = 4):
        self.kb = kb
        self.rules = list(BUILTIN_RULES)
        self.schema = schema
        self.max_depth = max_depth
        self.residuals: list[ProofResidual] = []
        self._in_progress: set = set()  # for cycle detection

    def _check_fact(self, pred: str, args: tuple) -> bool:
        if pred not in self.kb:
            return False
        facts = self.kb[pred]
        # Exact match
        if args in facts:
            return True
        # Handle variables (None)
        for stored_args in facts:
            if len(stored_args) == len(args):
                if all(a is None or a == s for a, s in zip(args, stored_args)):
                    return True
        return False

    def _unify(self, pattern: tuple, concrete: tuple) -> dict | None:
        """Simple unification: uppercase = variable, lowercase = constant."""
        if len(pattern) != len(concrete):
            return None
        bindings = {}
        for p, c in zip(pattern, concrete):
            if p.isupper():  # variable
                if p in bindings and bindings[p] != c:
                    return None
                bindings[p] = c
            else:
                if p != c:
                    return None
        return bindings

    def _apply_bindings(self, args: tuple, bindings: dict) -> tuple:
        return tuple(bindings.get(a, a) if a.isupper() else a for a in args)

    def prove(self, goal_pred: str, goal_args: tuple, depth: int = 0) -> bool | str:
        if depth > self.max_depth:
            return "RESIDUAL"

        # Cycle detection
        call_key = (goal_pred, goal_args, depth)
        if call_key in self._in_progress:
            return "RESIDUAL"
        self._in_progress.add(call_key)

        try:
            # Check KB facts
            if self._check_fact(goal_pred, goal_args):
                return True

            # Try inference rules
            for rule in self.rules:
                if rule.head_pred != goal_pred:
                    continue
                bindings = self._unify(rule.head_args, goal_args)
                if bindings is None:
                    continue
                # Try to prove body
                all_proved = True
                for body_pred, body_args in rule.body:
                    bound_args = self._apply_bindings(body_args, bindings)
                    r = self.prove(body_pred, bound_args, depth + 1)
                    if r != True:
                        all_proved = False
                        break
                if all_proved:
                    return True

            # Cannot prove — log as residual
            residual = ProofResidual(
                predicate=goal_pred,
                args=goal_args,
                type_constraints=self.schema.get(goal_pred, {}).get("types", ()),
                depth=depth,
                proof_context=[],
            )
            self.residuals.append(residual)
            return "RESIDUAL"

        finally:
            self._in_progress.discard(call_key)

    def get_residuals(self) -> list[ProofResidual]:
        seen = set()
        unique = []
        for r in self.residuals:
            key = (r.predicate, r.args)
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique[:50]  # cap at 50 per example


def residual_to_prompt(residual: ProofResidual, premises: str, schema: dict) -> str:
    pred_info = schema.get(residual.predicate, {})
    nl_template = pred_info.get("nl", f"{{0}} {residual.predicate} {{1}}")
    args_str = [str(a) if a else "someone" for a in residual.args]
    # Pad args if needed
    while len(args_str) < nl_template.count("{"):
        args_str.append("something")
    try:
        nl = nl_template.format(*args_str)
    except (IndexError, KeyError):
        nl = f"{residual.predicate}({', '.join(args_str)})"
    return BINARY_PROMPT.format(premises=premises[:1500], predicate_nl=nl)


# ── Gold Predicate Extractor ──────────────────────────────────────────────────
def parse_gold_predicates(ex: dict) -> dict[tuple, bool]:
    """Extract gold predicate truth values from FOL annotations."""
    gold: dict[tuple, bool] = {}
    fol_fields = []
    if ex.get("premises-FOL"):
        fol_fields.append((ex["premises-FOL"], True))
    if ex.get("conclusion-FOL"):
        # Conclusion truth depends on label
        label = ex.get("label", "Uncertain")
        truth = label == "True"
        fol_fields.append((ex["conclusion-FOL"], truth))

    for fol_str, truth_val in fol_fields:
        for pred, args in extract_predicate_atoms(fol_str):
            norm_args = normalize_args(args)
            key = (pred, norm_args)
            gold[key] = truth_val
    return gold


# ── Metrics ───────────────────────────────────────────────────────────────────
def compute_pilot_metrics(
    parsed: list[dict],
    pilot_set: list[dict],
    mode: str,
    split_idx: int = 100,
) -> dict:
    def metrics_for_subset(p_list, ex_list):
        tp = fp = tn = fn = 0
        grounded = 0
        n = len(p_list)
        for p, ex in zip(p_list, ex_list):
            gold = ex.get("gold_truth_value", "YES")
            if mode == "binary":
                pred = p.get("truth_value", "UNCERTAIN")
                if p.get("grounded", False):
                    grounded += 1
            else:  # fol mode — check if any predicate matches gold
                pred = "YES" if p else "UNCERTAIN"

            if gold == "YES" and pred == "YES":
                tp += 1
            elif gold == "YES" and pred != "YES":
                fn += 1
            elif gold == "NO" and pred == "NO":
                tn += 1
            elif gold == "NO" and pred == "YES":
                fp += 1
            # UNCERTAIN: partial credit
            elif pred == "UNCERTAIN":
                pass  # neutral

        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        return {
            "precision": float(precision),
            "recall": float(recall),
            "grounding_rate": float(grounded / max(n, 1)),
            "n": n,
        }

    return {
        "train": metrics_for_subset(parsed[:split_idx], pilot_set[:split_idx]),
        "held": metrics_for_subset(parsed[split_idx:], pilot_set[split_idx:]),
        "all": metrics_for_subset(parsed, pilot_set),
    }


# ── Data Loading ──────────────────────────────────────────────────────────────
def load_folio(split: str = "validation") -> list[dict]:
    from datasets import load_dataset
    ds = load_dataset("tasksource/folio", split=split)
    examples = []
    for ex in ds:
        examples.append({
            "story_id": ex["story_id"],
            "example_id": ex["example_id"],
            "premises": ex["premises"] or "",
            "premises_fol": ex.get("premises-FOL", "") or "",
            "conclusion": ex["conclusion"] or "",
            "conclusion_fol": ex.get("conclusion-FOL", "") or "",
            "label": ex["label"] or "Uncertain",
        })
    return examples


def sample_pilot_set(folio_train: list[dict], n: int = 200) -> list[dict]:
    """Build pilot set from FOLIO train with gold truth values extracted from FOL."""
    pilot = []
    for ex in folio_train[:n]:
        # Extract gold predicate truth values from FOL annotations
        fol_str = ex.get("premises_fol", "") + " " + ex.get("conclusion_fol", "")
        gold_preds = extract_predicate_atoms(fol_str)

        if gold_preds:
            # Pick the first concrete predicate as target
            pred, args = gold_preds[0]
            norm_args = normalize_args(args)
            # Gold truth: if this pred appears in premises-FOL, it's stated as TRUE
            gold_truth = "YES" if pred in ex.get("premises_fol", "").lower() else "NO"
        else:
            pred = "is_a"
            norm_args = ("entity", "category")
            gold_truth = "UNCERTAIN"

        pred_info = SEED_SCHEMA.get(
            fuzzy_match_predicate(pred, SEED_SCHEMA) or pred,
            {"nl": f"{{0}} {pred} {{1}}"},
        )
        nl_template = pred_info.get("nl", f"{{0}} {pred} {{1}}")
        try:
            predicate_nl = nl_template.format(*norm_args)
        except (IndexError, KeyError):
            predicate_nl = f"{pred}({', '.join(norm_args)})"

        pilot.append({
            "example_id": ex["example_id"],
            "premises_text": ex["premises"],
            "conclusion": ex["conclusion"],
            "hypothesis": ex["conclusion"],
            "target_predicate": pred,
            "predicate_nl": predicate_nl,
            "gold_truth_value": gold_truth,
            "label": ex["label"],
            "source_dataset": "FOLIO_train",
            "conclusion_fol": ex.get("conclusion_fol", ""),
        })
    return pilot


# ── Main Pipeline ─────────────────────────────────────────────────────────────
@logger.catch(reraise=True)
def main():
    results = {}

    # ── Load datasets ──────────────────────────────────────────────────────
    logger.info("Loading FOLIO datasets...")
    folio_val = load_folio("validation")   # 203 examples
    folio_train = load_folio("train")      # 1001 examples
    logger.info(f"FOLIO val: {len(folio_val)}, train: {len(folio_train)}")

    # Build pilot set from FOLIO train (as CLUTRR/ProofWriter not accessible)
    pilot_set = sample_pilot_set(folio_train, n=200)
    pilot_train_set = pilot_set[:100]
    pilot_held_set = pilot_set[100:200]

    # ── STAGE 0: Pilot — Binary vs Open-Ended FOL ─────────────────────────
    logger.info("=== STAGE 0: Pilot elicitation ===")

    binary_prompts = [
        BINARY_PROMPT.format(
            premises=ex["premises_text"][:1500],
            predicate_nl=ex["predicate_nl"],
        )
        for ex in pilot_set
    ]
    fol_prompts = [
        OPEN_FOL_PROMPT.format(
            premises=ex["premises_text"][:1500],
            hypothesis=ex["hypothesis"],
        )
        for ex in pilot_set
    ]

    logger.info(f"Stage 0: {len(binary_prompts)} binary + {len(fol_prompts)} FOL prompts")

    binary_responses = asyncio.run(
        call_llm_batch(binary_prompts, system=SYSTEM_BINARY, max_tokens=100)
    )
    logger.info(f"Binary responses done. Cost so far: ${cost_tracker.cumulative_usd:.4f}")

    fol_responses = asyncio.run(
        call_llm_batch(fol_prompts, system=SYSTEM_REASONING, max_tokens=200)
    )
    logger.info(f"FOL responses done. Cost so far: ${cost_tracker.cumulative_usd:.4f}")

    binary_parsed = [
        parse_binary_response(r, ex["premises_text"])
        for r, ex in zip(binary_responses, pilot_set)
    ]
    fol_parsed = [parse_fol_response(r) for r in fol_responses]

    binary_metrics = compute_pilot_metrics(binary_parsed, pilot_set, mode="binary")
    fol_metrics = compute_pilot_metrics(fol_parsed, pilot_set, mode="fol")

    p_llm = binary_metrics["train"]["precision"]
    p_world = p_llm * 0.7

    results["stage0_pilot"] = {
        "binary_precision_train": binary_metrics["train"]["precision"],
        "binary_precision_held": binary_metrics["held"]["precision"],
        "binary_recall_held": binary_metrics["held"]["recall"],
        "fol_precision_held": fol_metrics["held"]["precision"],
        "fol_recall_held": fol_metrics["held"]["recall"],
        "precision_delta_pp": (binary_metrics["held"]["precision"] - fol_metrics["held"]["precision"]) * 100,
        "p_llm": float(p_llm),
        "p_world": float(p_world),
        "binary_grounding_rate": binary_metrics["held"]["grounding_rate"],
        "n_binary_parseable": sum(1 for p in binary_parsed if p["truth_value"] != "UNCERTAIN"),
        "n_fol_parseable": sum(1 for p in fol_parsed if len(p) > 0),
    }
    logger.info(
        f"[STAGE 0] Binary prec={binary_metrics['held']['precision']:.3f}, "
        f"FOL prec={fol_metrics['held']['precision']:.3f}, "
        f"delta={results['stage0_pilot']['precision_delta_pp']:.1f}pp"
    )

    # ── STAGE 1: Schema Coverage Audit ────────────────────────────────────
    logger.info("=== STAGE 1: Schema Coverage Audit ===")
    coverage_results = {}
    for dname, exs in [("FOLIO_val", folio_val), ("FOLIO_train_sample", folio_train[:200])]:
        coverage_results[dname] = schema_coverage_audit(exs, SEED_SCHEMA)
        logger.info(
            f"[STAGE 1] {dname}: coverage={coverage_results[dname]['mean_coverage']:.2%}"
        )

    results["stage1_schema_coverage"] = coverage_results

    # ── STAGE 2: Residual Count Measurement ───────────────────────────────
    logger.info("=== STAGE 2: Residual Count Measurement ===")

    nlp = get_nlp()  # preload
    residual_stats_list = []

    for ex in folio_val:
        try:
            entities = extract_entities(ex["premises"])
            kb = build_initial_kb(ex["premises"], entities)
            atoms = extract_predicate_atoms(ex["conclusion_fol"] or ex["conclusion"])
            interp = BackwardChainingInterpreter(kb, SEED_SCHEMA)
            for pred, args in atoms:
                norm = normalize_args(args)
                interp.prove(pred, norm)
            residuals = interp.get_residuals()
            n_ent = len(entities)
            analytical_bound = P * (max(n_ent, 1) ** 2) * 3
            residual_stats_list.append({
                "n_residuals": len(residuals),
                "n_entities": n_ent,
                "analytical_bound": analytical_bound,
                "example_id": ex["example_id"],
            })
        except Exception:
            logger.error(f"Stage 2 error on example {ex.get('example_id', '?')}")

    r_counts = [s["n_residuals"] for s in residual_stats_list]
    bounds = [s["analytical_bound"] for s in residual_stats_list]

    results["stage2_residual_stats"] = {
        "mean": float(np.mean(r_counts)) if r_counts else 0,
        "median": float(np.median(r_counts)) if r_counts else 0,
        "p95": float(np.percentile(r_counts, 95)) if r_counts else 0,
        "max": int(max(r_counts)) if r_counts else 0,
        "analytical_bound_mean": float(np.mean(bounds)) if bounds else 0,
        "prune_ratio": float(1 - np.mean(r_counts) / max(np.mean(bounds), 1)) if r_counts else 0,
    }
    logger.info(
        f"[STAGE 2] Residuals mean={results['stage2_residual_stats']['mean']:.1f}, "
        f"p95={results['stage2_residual_stats']['p95']:.0f}"
    )

    # ── STAGE 3+4: Full Pipeline on FOLIO val + FOLIO train sample ────────
    logger.info("=== STAGE 3+4: Full Pipeline Evaluation ===")

    eval_datasets = [
        ("FOLIO_validation", folio_val),
        ("FOLIO_train_OOD", folio_train[:200]),
    ]

    pipeline_results = {}
    all_output_examples: list[dict] = []

    for dataset_name, examples in eval_datasets:
        if cost_tracker.cumulative_usd > 7.0:
            logger.warning("Budget approaching limit, stopping evaluation")
            break

        logger.info(f"\n[STAGE 3+4] Processing {dataset_name} ({len(examples)} examples)...")

        # Batch residual collection (no LLM needed)
        all_residuals_per_example = []
        for ex in examples:
            try:
                entities = extract_entities(ex["premises"])
                kb = build_initial_kb(ex["premises"], entities)
                atoms = extract_predicate_atoms(
                    ex.get("conclusion_fol", "") or ex.get("conclusion", "")
                )
                interp = BackwardChainingInterpreter(kb, SEED_SCHEMA)
                for pred, args in atoms:
                    norm = normalize_args(args)
                    interp.prove(pred, norm)
                residuals = interp.get_residuals()
                all_residuals_per_example.append((ex, residuals, kb))
            except Exception:
                logger.error(f"Residual error example {ex.get('example_id', '?')}")
                all_residuals_per_example.append((ex, [], {}))

        # Build flat prompt list for all residuals
        flat_prompts = []
        flat_meta = []
        for ex_idx, (ex, residuals, kb) in enumerate(all_residuals_per_example):
            for r_idx, residual in enumerate(residuals):
                prompt = residual_to_prompt(residual, ex["premises"], SEED_SCHEMA)
                flat_prompts.append(prompt)
                flat_meta.append((ex_idx, r_idx, residual))

        logger.info(f"  Calling LLM for {len(flat_prompts)} residuals...")
        if flat_prompts:
            flat_responses = asyncio.run(
                call_llm_batch(flat_prompts, system=SYSTEM_BINARY, max_tokens=100)
            )
        else:
            flat_responses = []

        # Parse responses and assign provenance
        elicited_map: dict[int, dict] = {}
        for (ex_idx, r_idx, residual), response in zip(flat_meta, flat_responses):
            parsed = parse_binary_response(
                response, all_residuals_per_example[ex_idx][0]["premises"]
            )
            if ex_idx not in elicited_map:
                elicited_map[ex_idx] = {}
            provenance = "LLM-GROUNDED" if parsed["grounded"] else "LLM-WORLD"
            confidence = p_llm if provenance == "LLM-GROUNDED" else p_world
            elicited_map[ex_idx][(residual.predicate, residual.args)] = {
                "truth_value": parsed["truth_value"],
                "span": parsed["span"],
                "grounded": parsed["grounded"],
                "provenance": provenance,
                "confidence": float(confidence),
            }

        # Stage 4: Proof execution
        pipeline_predictions = []

        for ex_idx, (ex, residuals, base_kb) in enumerate(all_residuals_per_example):
            # Build augmented KB
            augmented_kb = {k: dict(v) for k, v in base_kb.items()}
            for (pred, args), info in elicited_map.get(ex_idx, {}).items():
                if info["truth_value"] == "YES":
                    if pred not in augmented_kb:
                        augmented_kb[pred] = {}
                    augmented_kb[pred][args] = {
                        "provenance": info["provenance"],
                        "confidence": info["confidence"],
                        "span": info.get("span"),
                    }

            # Re-run backward chaining with full KB
            interp2 = BackwardChainingInterpreter(augmented_kb, SEED_SCHEMA)
            atoms = extract_predicate_atoms(
                ex.get("conclusion_fol", "") or ex.get("conclusion", "")
            )
            proof_result = "Uncertain"
            any_proved = False
            any_false = False

            for pred, args in atoms:
                norm = normalize_args(args)
                r = interp2.prove(pred, norm)
                if r == True:
                    any_proved = True
                # FOLIO: if goal can't be disproved, stay Uncertain

            if any_proved and len(atoms) > 0:
                # Check if all atoms proved
                all_proved = True
                new_interp = BackwardChainingInterpreter(augmented_kb, SEED_SCHEMA)
                for pred, args in atoms:
                    norm = normalize_args(args)
                    r = new_interp.prove(pred, norm)
                    if r != True:
                        all_proved = False
                        break
                if all_proved:
                    proof_result = "True"

            # Compute hallucination metrics
            gold_label = ex.get("label", "Uncertain")
            pipeline_correct = proof_result == gold_label

            n_llm_affirmed = 0
            factual_hallucinations = 0
            provenance_hallucinations = 0
            gold_preds = parse_gold_predicates(ex)

            for (pred, args), info in elicited_map.get(ex_idx, {}).items():
                if info["truth_value"] == "YES":
                    n_llm_affirmed += 1
                    norm_args = normalize_args(args)
                    gold_truth = gold_preds.get((pred, norm_args), None)
                    if gold_truth is not None and gold_truth != True:
                        factual_hallucinations += 1
                    if not info.get("grounded", False):
                        provenance_hallucinations += 1

            factual_hr = factual_hallucinations / max(n_llm_affirmed, 1)
            provenance_hr = provenance_hallucinations / max(n_llm_affirmed, 1)
            n_world = sum(
                1 for (p, a), inf in elicited_map.get(ex_idx, {}).items()
                if inf["provenance"] == "LLM-WORLD" and inf["truth_value"] == "YES"
            )
            risk_score = n_world / max(n_llm_affirmed, 1)

            pipeline_predictions.append({
                "example_id": ex.get("example_id", ex_idx),
                "gold_label": gold_label,
                "predicted_label": proof_result,
                "correct": bool(pipeline_correct),
                "n_residuals": len(residuals),
                "n_llm_affirmed": n_llm_affirmed,
                "factual_hallucination_rate": float(factual_hr),
                "provenance_hallucination_rate": float(provenance_hr),
                "hallucination_risk_score": float(risk_score),
                "premises_snippet": ex["premises"][:200],
                "conclusion": ex.get("conclusion", ""),
            })

        # Run baselines
        logger.info(f"  Running CoT baseline...")
        cot_prompts = [
            COT_PROMPT.format(
                premises=ex["premises"][:1200], hypothesis=ex["conclusion"]
            )
            for ex in examples
        ]
        cot_responses = asyncio.run(
            call_llm_batch(cot_prompts, system=SYSTEM_REASONING, max_tokens=300)
        )
        cot_preds = [parse_answer(r) for r in cot_responses]

        logger.info(f"  Running Logic-LM baseline...")
        logicLM_prompts = [
            LOGIC_LM_PROMPT.format(
                premises=ex["premises"][:1200], hypothesis=ex["conclusion"]
            )
            for ex in examples
        ]
        logicLM_responses = asyncio.run(
            call_llm_batch(logicLM_prompts, system=SYSTEM_REASONING, max_tokens=300)
        )
        logicLM_preds = [parse_answer(r) for r in logicLM_responses]

        accuracy = float(np.mean([p["correct"] for p in pipeline_predictions]))
        cot_acc = float(np.mean([
            pred == ex.get("label", "Uncertain")
            for pred, ex in zip(cot_preds, examples)
        ]))
        logicLM_acc = float(np.mean([
            pred == ex.get("label", "Uncertain")
            for pred, ex in zip(logicLM_preds, examples)
        ]))

        mean_factual_hr = float(np.mean([p["factual_hallucination_rate"] for p in pipeline_predictions]))
        mean_prov_hr = float(np.mean([p["provenance_hallucination_rate"] for p in pipeline_predictions]))
        risk_scores_list = [p["hallucination_risk_score"] for p in pipeline_predictions]
        corrects_list = [float(p["correct"]) for p in pipeline_predictions]

        spearman_rho, spearman_p = spearmanr(risk_scores_list, corrects_list)

        pipeline_results[dataset_name] = {
            "pipeline_accuracy": accuracy,
            "cot_accuracy": cot_acc,
            "logic_lm_accuracy": logicLM_acc,
            "accuracy_gain_vs_cot_pp": (accuracy - cot_acc) * 100,
            "accuracy_gain_vs_logicLM_pp": (accuracy - logicLM_acc) * 100,
            "factual_hallucination_rate": mean_factual_hr,
            "provenance_hallucination_rate": mean_prov_hr,
            "spearman_rho_risk_vs_accuracy": float(spearman_rho) if not np.isnan(spearman_rho) else 0.0,
            "spearman_p": float(spearman_p) if not np.isnan(spearman_p) else 1.0,
            "n_examples": len(examples),
            "total_residuals_processed": len(flat_prompts),
            "representative_proof_trees": pipeline_predictions[:10],
        }

        logger.info(
            f"  [{dataset_name}] Pipeline={accuracy:.3f}, CoT={cot_acc:.3f}, "
            f"LogicLM={logicLM_acc:.3f}, Δ_CoT={pipeline_results[dataset_name]['accuracy_gain_vs_cot_pp']:.1f}pp"
        )
        logger.info(
            f"  Hallucination: factual={mean_factual_hr:.3f}, prov={mean_prov_hr:.3f}, "
            f"Spearman ρ={spearman_rho:.3f} (p={spearman_p:.3f})"
        )

        # Build output examples for schema compliance
        for pred_info, ex, cot_p, lm_p in zip(
            pipeline_predictions, examples, cot_preds, logicLM_preds
        ):
            out_ex = {
                "input": f"Premises: {ex['premises']}\n\nHypothesis: {ex['conclusion']}",
                "output": ex["label"],
                "predict_pipeline": pred_info["predicted_label"],
                "predict_cot": cot_p,
                "predict_logic_lm": lm_p,
                "metadata_example_id": str(ex["example_id"]),
                "metadata_dataset": dataset_name,
                "metadata_n_residuals": str(pred_info["n_residuals"]),
                "metadata_n_llm_affirmed": str(pred_info["n_llm_affirmed"]),
                "metadata_factual_hallucination_rate": str(round(pred_info["factual_hallucination_rate"], 4)),
                "metadata_provenance_hallucination_rate": str(round(pred_info["provenance_hallucination_rate"], 4)),
                "metadata_hallucination_risk_score": str(round(pred_info["hallucination_risk_score"], 4)),
                "metadata_pipeline_correct": str(pred_info["correct"]),
            }
            all_output_examples.append((dataset_name, out_ex))

        # Free memory
        del flat_prompts, flat_responses, elicited_map, all_residuals_per_example
        del cot_prompts, cot_responses, logicLM_prompts, logicLM_responses
        gc.collect()

    results["stage3_4_pipeline"] = pipeline_results
    results["cost_tracker"] = {
        "total_usd": float(cost_tracker.cumulative_usd),
        "n_calls": cost_tracker.n_calls,
    }

    # ── Format output to exp_gen_sol_out schema ────────────────────────────
    # Group by dataset
    datasets_map: dict[str, list] = {}
    for ds_name, ex in all_output_examples:
        if ds_name not in datasets_map:
            datasets_map[ds_name] = []
        datasets_map[ds_name].append(ex)

    method_out = {
        "metadata": {
            "method_name": "Proof-Residual Directed Knowledge Elicitation",
            "description": (
                "5-stage pipeline: pilot comparison, schema coverage audit, "
                "residual-mode backward chaining, binary LLM elicitation, "
                "proof execution with provenance-annotated hallucination scoring."
            ),
            "primary_model": PRIMARY_MODEL,
            "stage0_pilot": results.get("stage0_pilot", {}),
            "stage1_schema_coverage": {
                k: {
                    "mean_coverage": v["mean_coverage"],
                    "n_examples": v["n_examples"],
                }
                for k, v in results.get("stage1_schema_coverage", {}).items()
            },
            "stage2_residual_stats": results.get("stage2_residual_stats", {}),
            "stage3_4_aggregate": {
                ds: {
                    k: v for k, v in m.items() if k != "representative_proof_trees"
                }
                for ds, m in results.get("stage3_4_pipeline", {}).items()
            },
            "cost_tracker": results.get("cost_tracker", {}),
        },
        "datasets": [
            {"dataset": ds_name, "examples": exs}
            for ds_name, exs in datasets_map.items()
        ],
    }

    # Save outputs
    out_path = WORKSPACE / "method_out.json"
    out_path.write_text(json.dumps(method_out, indent=2, default=str))
    logger.info(f"Saved method_out.json ({out_path.stat().st_size / 1e6:.2f} MB)")

    # Also save full results for reference
    full_results_path = WORKSPACE / "full_results.json"
    full_results_path.write_text(json.dumps(results, indent=2, default=str))
    logger.info(f"Saved full_results.json")

    logger.info(f"\nTotal cost: ${cost_tracker.cumulative_usd:.4f} ({cost_tracker.n_calls} calls)")
    logger.info("Pipeline complete!")


if __name__ == "__main__":
    main()

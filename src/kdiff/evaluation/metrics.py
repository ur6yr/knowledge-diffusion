"""Independent annotated metrics with complete-dialogue paired resampling."""

import random
from statistics import mean
from typing import Literal
from pydantic import Field, model_validator
from kdiff.core.contracts import Contract, digest
from kdiff.analysis.witness import replay


class Annotation(Contract):
    item_id: str
    run_ids: list[str] = Field(min_length=1)
    variant: str
    asserted: int = Field(ge=0)
    correct: int = Field(ge=0)
    unsupported: int = Field(ge=0)
    required: int = Field(ge=0)
    recovered: int = Field(ge=0)
    partial_candidates: int = Field(ge=0)
    partial_handled: int = Field(ge=0)
    refusals: int = Field(ge=0)
    correct_refusals: int = Field(ge=0)
    witness_ids: list[str]
    identity_correct: bool | None = None
    temporal_correct: bool | None = None
    plan_valid_initial: bool | None = None
    plan_valid_after_repair: bool | None = None
    reviewer: str
    review_method: Literal['independent_human', 'independent_adjudicated_gold']
    annotation_artifact: str
    reviewed_at: str

    @model_validator(mode='after')
    def valid(self):
        from datetime import datetime
        datetime.fromisoformat(self.reviewed_at)
        if not self.reviewer.strip():
            raise ValueError('Independent reviewer provenance is required')
        if (self.correct + self.unsupported > self.asserted or self.recovered > self.required
                or self.partial_handled > self.partial_candidates or self.correct_refusals > self.refusals):
            raise ValueError('Inconsistent metric numerators and denominators')
        return self


def fraction(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def aggregate(rows):
    pairs = {'claim_correctness': ('correct', 'asserted'), 'unsupported_claim_rate': ('unsupported', 'asserted'),
             'required_claim_recall': ('recovered', 'required'), 'partial_handling': ('partial_handled', 'partial_candidates'),
             'correct_refusal_rate': ('correct_refusals', 'refusals')}
    output = {key: fraction(sum(r[a] for r in rows), sum(r[b] for r in rows)) for key, (a, b) in pairs.items()}
    for key in ['identity_correct', 'temporal_correct', 'plan_valid_initial', 'plan_valid_after_repair']:
        available = [r[key] for r in rows if r[key] is not None]
        output[key] = fraction(sum(available), len(available))
    for status in ['Answer', 'Clarify', 'Abstain']:
        output[status.lower() + '_coverage'] = fraction(sum(r['outcome'] == status for r in rows), len(rows))
    output['witness_validity'] = fraction(sum(r['valid_witnesses'] for r in rows), sum(len(r['witness_ids']) for r in rows))
    return output


def evaluate(store, benchmark_id, annotations):
    benchmark = store.get(benchmark_id)
    if benchmark.get('kind') != 'benchmark-v1':
        raise ValueError('Unsupported benchmark manifest')
    items = {x['item_id']: x for x in benchmark['items']}
    rows, seen = [], set()
    for raw in annotations:
        annotation = Annotation.model_validate(raw)
        if annotation.item_id not in items or (annotation.item_id, annotation.variant) in seen:
            raise ValueError('Unknown item or duplicate item/variant annotation')
        seen.add((annotation.item_id, annotation.variant))
        item = items[annotation.item_id]
        if annotation.required != len(item['required_claims']):
            raise ValueError('Required-claim denominator disagrees with benchmark gold')
        if len(annotation.run_ids) != len(item['turns']):
            raise ValueError('Every dialogue turn requires its stored execution')
        store.get_bytes(annotation.annotation_artifact)
        runs = [store.get(rid) for rid in annotation.run_ids]
        outcomes = [run.get('outcome', {}) for run in runs]
        for run, outcome in zip(runs, outcomes):
            if (outcome.get('release_id') or outcome.get('state', {}).get('release_id') or run.get('release_id')) != item['release_id']:
                raise ValueError('Evaluation run used a different snapshot')
            recorded_variant = run.get('experiment', {}).get('variant', 'full')
            if recorded_variant != annotation.variant:
                raise ValueError('Ablation cannot reuse a full-method final answer')
        stored_claims = sum(len(outcome.get('claims', [])) for outcome in outcomes)
        if annotation.asserted != stored_claims:
            raise ValueError('Factual assertion denominator disagrees with stored executions')
        if annotation.refusals != sum(len(o.get('refusals', [])) for o in outcomes):
            raise ValueError('Refusal denominator disagrees with stored executions')
        stored_witnesses = {wid for outcome in outcomes for wid in outcome.get('witnesses', [])}
        if set(annotation.witness_ids) != stored_witnesses or len(annotation.witness_ids) != len(stored_witnesses):
            raise ValueError('Witness denominator must include every final assigned witness')
        valid, failures = 0, []
        for wid in annotation.witness_ids:
            try:
                replay(store, wid)
                valid += 1
            except (ValueError, KeyError, FileNotFoundError) as exc:
                failures.append({'witness': wid, 'error': type(exc).__name__})
        # A dialogue receives Answer coverage only when all dependent turns answer.
        status = 'Answer' if all(o.get('type') == 'Answer' for o in outcomes) else (
            'Abstain' if any(o.get('type') == 'Abstain' for o in outcomes) else 'Clarify')
        elapsed = [run.get('elapsed_seconds') for run in runs]
        rows.append({**annotation.model_dump(mode='json'), 'outcome': status,
                     'valid_witnesses': valid, 'witness_failures': failures,
                     'elapsed_seconds': sum(elapsed) if all(x is not None for x in elapsed) else None,
                     'budget_snapshots': [run.get('budget') for run in runs],
                     'budget_scope': 'Recorded snapshots may include earlier stages in a shared job. Do not sum cumulative costs.'})
    if not rows:
        raise ValueError('No independent annotations. No quality scores produced')
    by_variant = {v: aggregate([r for r in rows if r['variant'] == v]) for v in sorted({r['variant'] for r in rows})}
    result = {'kind': 'evaluation-v1', 'benchmark_id': benchmark_id, 'origin': benchmark['origin'],
              'rows': rows, 'metrics': by_variant, 'unannotated_items': {
                  v: sorted(set(items) - {r['item_id'] for r in rows if r['variant'] == v}) for v in by_variant},
              'denominator_policy': 'Refusals excluded from factual assertions. Zero denominators are null. Dialogues stay clustered.'}
    return {'evaluation_id': store.put(result), **result}


def paired_bootstrap(left, right, *, metric='claim_correctness', seed=2026, resamples=10000):
    """Paired complete-item bootstrap with identical sampled IDs in both arms."""
    a, b = {r['item_id']: r for r in left}, {r['item_id']: r for r in right}
    if not a or set(a) != set(b) or len(a) != len(left) or len(b) != len(right):
        raise ValueError('Paired bootstrap requires identical unique item sets')
    if not 100 <= resamples <= 100000:
        raise ValueError('Bootstrap resamples out of bounds')
    ids = sorted(a)
    rng = random.Random(seed)
    differences, undefined = [], 0
    for _ in range(resamples):
        chosen = rng.choices(ids, k=len(ids))
        x = aggregate([a[i] for i in chosen])[metric]['value']
        y = aggregate([b[i] for i in chosen])[metric]['value']
        if x is None or y is None:
            undefined += 1
        else:
            differences.append(y - x)
    differences.sort()
    def quantile(q):
        if not differences:
            return None
        index = (len(differences) - 1) * q
        lo, hi = int(index), min(int(index) + 1, len(differences) - 1)
        return differences[lo] + (differences[hi] - differences[lo]) * (index - lo)
    return {'metric': metric, 'direction': 'right_minus_left', 'seed': seed, 'resamples': resamples,
            'item_count': len(ids), 'undefined_resamples': undefined,
            'interval_95_percentile': [quantile(.025), quantile(.975)],
            'mean_difference': mean(differences) if differences else None}

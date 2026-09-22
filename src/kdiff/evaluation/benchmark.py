"""Explicit original/synthetic benchmark import without reconstructed gold answers."""

import json
from collections import Counter
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from kdiff.core.contracts import Contract, Window, digest
from kdiff.analysis.witness import load_release

FAMILIES = {'entity_resolution': 28, 'temporal_existence': 32, 'mobility_sequence': 26,
            'pre_post_comparison': 38, 'diffusion_path': 30, 'patent_pathway': 21, 'multi_turn_dialogue': 40}


class Item(Contract):
    item_id: str
    family: Literal['entity_resolution', 'temporal_existence', 'mobility_sequence', 'pre_post_comparison',
                    'diffusion_path', 'patent_pathway', 'multi_turn_dialogue']
    turns: list[str] = Field(min_length=1, max_length=20)
    difficulty: str
    exposure_stratum: str
    release_id: str
    reference_date: str
    gold_entities: list[str]
    gold_windows: list[Window]
    required_claims: list[dict]
    acceptable_rewrites: list[dict]
    minimum_witnesses: list[dict]
    annotation_provenance: dict

    @model_validator(mode='after')
    def valid(self):
        from datetime import date
        date.fromisoformat(self.reference_date)
        if self.family != 'multi_turn_dialogue' and len(self.turns) != 1:
            raise ValueError('Dependent turns must be one multi-turn dialogue item')
        if not self.annotation_provenance.get('source'):
            raise ValueError('Gold annotation provenance is required')
        if any(w.reference_date.isoformat() != self.reference_date for w in self.gold_windows):
            raise ValueError('Gold window reference dates disagree')
        return self


def import_benchmark(path, store, *, name, origin):
    path = Path(path)
    if not path.is_file():
        raise ValueError('Benchmark artifact absent. No original questions or scores can be reconstructed')
    if origin not in {'original', 'synthetic', 'independent'}:
        raise ValueError('Benchmark origin must be explicit')
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Benchmark import exceeds 16 MiB')
    raw = path.read_bytes()
    items = [Item.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    if not 1 <= len(items) <= 10000 or len({x.item_id for x in items}) != len(items):
        raise ValueError('Invalid benchmark size or duplicate item IDs')
    families = dict(Counter(x.family for x in items))
    if origin == 'original' and families != FAMILIES:
        raise ValueError('Original benchmark must contain the described 215-item family structure')
    for release_id in {x.release_id for x in items}:
        release = load_release(store, release_id)
        if origin == 'original' and release['synthetic']:
            raise ValueError('Synthetic graph cannot represent the original benchmark snapshot')
        ids = {e['canonical_id'] for e in release['graph']['entities']}
        for item in [x for x in items if x.release_id == release_id]:
            if set(item.gold_entities) - ids:
                raise ValueError('Gold identity absent from pinned release')
    manifest = {'kind': 'benchmark-v1', 'name': name, 'origin': origin,
                'input': store.capture(raw), 'items': [x.model_dump(mode='json') for x in items],
                'families': families, 'item_count': len(items), 'scoring_unit': 'complete_item_or_dialogue'}
    return {'benchmark_id': store.put(manifest), 'origin': origin, 'item_count': len(items)}


def experiment_key(benchmark_id, item_id, variant, profile_fingerprint, seed):
    """Final-answer caches cannot leak across variants or experimental seeds."""
    return digest({'benchmark': benchmark_id, 'item': item_id, 'variant': variant,
                   'profile': profile_fingerprint, 'seed': seed})

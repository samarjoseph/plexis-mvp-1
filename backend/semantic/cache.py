"""
Semantic Cache

Caches the generated ontology to avoid repeated LLM calls for the same dataset.
We use a simple in-memory dictionary backed by a JSON file.
"""
import json
import os
import logging
from typing import Optional, Dict, Any
from .ontology import DatasetOntology, ColumnOntology

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "semantic_cache.json")
_memory_cache: Dict[str, DatasetOntology] = {}
_cache_loaded = False

def _load_cache():
    global _cache_loaded, _memory_cache
    if _cache_loaded:
        return
        
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            for ds_name, ds_data in data.items():
                cols = {}
                cols_raw = ds_data.get("columns")
                cols_dict = cols_raw if isinstance(cols_raw, dict) else {}
                for col_name, col_data in cols_dict.items():
                    if isinstance(col_data, dict):
                        cols[col_name] = ColumnOntology(**col_data)
                    
                ontology = DatasetOntology(
                    dataset_name=ds_data.get("dataset_name", ds_name),
                    domain=ds_data.get("domain", "Generic"),
                    columns=cols,
                    derived_metrics=ds_data.get("derived_metrics", []),
                    relationships=ds_data.get("relationships", {})
                )
                _memory_cache[ds_name] = ontology
        except Exception as e:
            logger.error(f"Failed to load semantic cache: {e}")
            
    _cache_loaded = True

def _save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        data = {k: v.to_dict() for k, v in _memory_cache.items()}
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save semantic cache: {e}")

def get_cached_ontology(dataset_name: str) -> Optional[DatasetOntology]:
    """Retrieve the ontology for a dataset if it exists."""
    _load_cache()
    return _memory_cache.get(dataset_name)

def cache_ontology(ontology: DatasetOntology):
    """Save the ontology to the cache."""
    _load_cache()
    _memory_cache[ontology.dataset_name] = ontology
    _save_cache()

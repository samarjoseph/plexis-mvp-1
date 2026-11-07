"""
DKO Serializer — DatasetKnowledgeObject ↔ JSON-safe dict for PostgreSQL JSONB.

The DKO already has a to_dict() method that handles Enum.value conversion.
This serializer wraps that, adds numpy/datetime safety, and provides
the deserialization path to reconstruct a DKO from stored JSONB.

Round-trip guarantee:
    dko → serialize() → json-safe dict → PostgreSQL JSONB
    PostgreSQL JSONB → dict → deserialize() → reconstructed DatasetKnowledgeObject

The deserialized DKO is sufficient for:
    - Restoring dataset context for LLM prompts
    - Restoring schema/profile information
    - Restoring analysis capabilities detection
    - Reconstructing ModuleRegistry entries
"""
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _safe_value(val: Any) -> Any:
    """
    Recursively convert a value to a JSON-safe primitive.

    Handles:
    - numpy scalars (int64, float64, bool_) → Python native
    - numpy arrays → list
    - datetime objects → ISO 8601 string
    - Enum → .value
    - dataclasses with to_dict() → dict (via to_dict())
    - sets/tuples → list
    - None → None (passthrough)
    - dicts/lists → recursively processed
    - Everything else → str() as fallback
    """
    if val is None:
        return None

    # Avoid importing numpy at module load — only import if needed
    type_name = type(val).__name__
    module_name = type(val).__module__ or ""

    # numpy scalars
    if module_name.startswith("numpy"):
        try:
            import numpy as np
            if isinstance(val, np.integer):
                return int(val)
            if isinstance(val, np.floating):
                return float(val)
            if isinstance(val, np.bool_):
                return bool(val)
            if isinstance(val, np.ndarray):
                return [_safe_value(v) for v in val.tolist()]
        except ImportError:
            pass

    # Python native types — passthrough
    if isinstance(val, (bool, int, float, str)):
        return val

    # datetime / date / time
    import datetime
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()

    # Enum
    from enum import Enum
    if isinstance(val, Enum):
        return val.value

    # Objects with to_dict() method (all DKO sub-objects have this)
    if hasattr(val, "to_dict") and callable(val.to_dict):
        return _safe_dict(val.to_dict())

    # dict
    if isinstance(val, dict):
        return _safe_dict(val)

    # list / tuple / set
    if isinstance(val, (list, tuple, set, frozenset)):
        return [_safe_value(v) for v in val]

    # Fallback — convert to string representation
    logger.debug("[DKO_SERIALIZER] Non-JSON-safe type %s; using str() fallback", type(val).__name__)
    return str(val)


def _safe_dict(d: dict) -> dict:
    """Recursively make a dict JSON-safe."""
    return {str(k): _safe_value(v) for k, v in d.items()}


class DKOSerializer:
    """
    Serializes and deserializes DatasetKnowledgeObject for PostgreSQL JSONB storage.

    serialize() → call dko.to_dict() (which handles Enum.value) then apply
                  _safe_value() pass for any remaining numpy/datetime types.

    deserialize() → reconstructs a DatasetKnowledgeObject from stored dict.
                    Uses the DKO's own from_dict() if available,
                    otherwise falls back to best-effort field reconstruction.
    """

    def serialize(self, dko) -> Dict[str, Any]:
        """
        Convert a DatasetKnowledgeObject to a JSON-safe dict for JSONB storage.

        Uses dko.to_dict() as the primary serialization path, then applies
        _safe_value() to catch any remaining numpy/datetime types that
        to_dict() may have missed (e.g., in stats objects).
        """
        try:
            raw = dko.to_dict()
            safe = _safe_dict(raw)
            return safe
        except Exception as e:
            logger.error("[DKO_SERIALIZER] serialize() failed: %s", e, exc_info=True)
            raise

    def deserialize(self, data: Dict[str, Any]):
        """
        Reconstruct a DatasetKnowledgeObject from a stored JSONB dict.

        Returns a DatasetKnowledgeObject if successful.
        Falls back to a lightweight stub on failure (so the system degrades
        gracefully rather than crashing — the DKO can be regenerated).
        """
        if not data:
            return None

        try:
            return self._reconstruct_dko(data)
        except Exception as e:
            logger.warning(
                "[DKO_SERIALIZER] Full deserialization failed (%s); using lightweight stub. "
                "DKO will be regenerated on next analysis.",
                e,
            )
            return self._make_stub(data)

    def _reconstruct_dko(self, data: Dict[str, Any]):
        """Full reconstruction of DKO from stored dict."""
        from dataset_intelligence.models_v2 import (
            DatasetKnowledgeObject,
            IntelligenceStackVersion,
            DatasetIdentity,
            DatasetObservation,
            InferenceMetadata,
            ColumnIntelligence,
            AnalyticalGroup,
            AnalyticalOpportunity,
            KnowledgeGraph,
            Dependency,
            GroupingRelationship,
        )
        from dataset_intelligence.models import (
            ColumnRole, ColumnSemanticType, DataInsight, PredictedQuestion,
            NumericStats, CategoricalStats, DatetimeStats, ColumnCorrelation,
            QualityReport, DatasetPresentation, InsightSeverity
        )

        def _meta(d):
            if not d:
                return InferenceMetadata()
            return InferenceMetadata(
                confidence=d.get("confidence", 1.0),
                importance=d.get("importance", 0.0),
                explanation=d.get("explanation", ""),
                lineage=d.get("lineage", ""),
            )

        def _col(c):
            if not c:
                return None
            # Numeric stats
            ns_d = c.get("numeric_stats")
            ns = None
            if ns_d:
                try:
                    ns = NumericStats(**{k: v for k, v in ns_d.items() if k in NumericStats.__dataclass_fields__})
                except Exception:
                    pass

            # Categorical stats
            cs_d = c.get("categorical_stats")
            cs = None
            if cs_d:
                try:
                    cs = CategoricalStats(**{k: v for k, v in cs_d.items() if k in CategoricalStats.__dataclass_fields__})
                except Exception:
                    pass

            return ColumnIntelligence(
                name=c.get("name", ""),
                position=c.get("position", 0),
                dtype_raw=c.get("dtype_raw", ""),
                dtype_category=c.get("dtype_category", ""),
                role=ColumnRole(c.get("role", "Unknown")),
                semantic_type=ColumnSemanticType(c.get("semantic_type", "unknown")),
                business_meaning=c.get("business_meaning"),
                null_count=c.get("null_count", 0),
                null_pct=c.get("null_pct", 0.0),
                unique_count=c.get("unique_count", 0),
                unique_pct=c.get("unique_pct", 0.0),
                is_nullable=c.get("is_nullable", False),
                is_constant=c.get("is_constant", False),
                is_unique=c.get("is_unique", False),
                numeric_stats=ns,
                categorical_stats=cs,
                characteristics=c.get("characteristics", []),
                metadata=_meta(c.get("metadata")),
            )

        def _dep(d):
            return Dependency(
                source=d.get("source", ""),
                target=d.get("target", ""),
                relationship_type=d.get("relationship_type", ""),
                metadata=_meta(d.get("metadata")),
            )

        def _grp(g):
            return GroupingRelationship(
                dimension=g.get("dimension", ""),
                metrics=g.get("metrics", []),
                metadata=_meta(g.get("metadata")),
            )

        kg_raw = data.get("knowledge_graph")
        kg_d = kg_raw if isinstance(kg_raw, dict) else {}
        kg = KnowledgeGraph(
            hierarchies=[_dep(h) for h in kg_d.get("hierarchies", [])],
            dependencies=[_dep(d) for d in kg_d.get("dependencies", [])],
            groupings=[_grp(g) for g in kg_d.get("groupings", [])],
            correlations=[],  # ColumnCorrelation reconstruction skipped — not critical
        )

        id_raw = data.get("identity")
        identity_d = id_raw if isinstance(id_raw, dict) else {}
        identity = DatasetIdentity(
            probable_purpose=identity_d.get("probable_purpose", ""),
            primary_entities=identity_d.get("primary_entities", []),
            business_concepts=identity_d.get("business_concepts", []),
            overall_readiness=identity_d.get("overall_readiness", "unknown"),
            kpis=identity_d.get("kpis", []),
            strongest_groupings=identity_d.get("strongest_groupings", []),
        )

        observations = [
            DatasetObservation(
                observation=o.get("observation", ""),
                category=o.get("category", ""),
                metadata=_meta(o.get("metadata")),
            )
            for o in data.get("observations", [])
        ]

        columns = {}
        cols_raw = data.get("columns")
        cols_dict = cols_raw if isinstance(cols_raw, dict) else {}
        for name, col_data in cols_dict.items():
            col = _col(col_data)
            if col:
                columns[name] = col

        opportunities = [
            AnalyticalOpportunity(
                name=o.get("name", ""),
                description=o.get("description", ""),
                required_columns=o.get("required_columns", []),
                metadata=_meta(o.get("metadata")),
            )
            for o in data.get("opportunities", [])
        ]

        analytical_groups = [
            AnalyticalGroup(
                name=g.get("name", ""),
                group_type=g.get("group_type", ""),
                columns=g.get("columns", []),
                metadata=_meta(g.get("metadata")),
            )
            for g in data.get("analytical_groups", [])
        ]

        # Insights and predicted_questions — reconstruct minimally
        insights = []
        for i in data.get("insights", []):
            try:
                insights.append(DataInsight(
                    title=i.get("title", ""),
                    description=i.get("description", ""),
                    insight_type=i.get("insight_type", ""),
                    importance_score=i.get("importance_score", 0.0),
                    columns=i.get("columns", []),
                    severity=InsightSeverity(i.get("severity", "info")),
                ))
            except Exception:
                pass

        predicted_questions = []
        for q in data.get("predicted_questions", []):
            try:
                predicted_questions.append(PredictedQuestion(
                    question=q.get("question", ""),
                    category=q.get("category", ""),
                    complexity=q.get("complexity", "simple"),
                    priority=q.get("priority", 0.0),
                ))
            except Exception:
                pass

        versions_d = data.get("versions", {})
        from dataset_intelligence.models_v2 import IntelligenceStackVersion
        versions = IntelligenceStackVersion(
            engine_version=versions_d.get("engine_version", "1.0.0"),
            dko_schema_version=versions_d.get("dko_schema_version", "2.0.0"),
            knowledge_graph_version=versions_d.get("knowledge_graph_version", "1.0.0"),
            semantic_model_version=versions_d.get("semantic_model_version", "1.0.0"),
            observation_engine_version=versions_d.get("observation_engine_version", "1.0.0"),
        )

        return DatasetKnowledgeObject(
            fingerprint=data.get("fingerprint", ""),
            dataset_name=data.get("dataset_name", ""),
            analyzed_at=data.get("analyzed_at", ""),
            row_count=data.get("row_count", 0),
            column_count=data.get("column_count", 0),
            memory_usage_bytes=data.get("memory_usage_bytes", 0),
            versions=versions,
            identity=identity,
            observations=observations,
            columns=columns,
            analytical_groups=analytical_groups,
            knowledge_graph=kg,
            opportunities=opportunities,
            insights=insights,
            predicted_questions=predicted_questions,
            quality_report=None,    # Complex reconstruction — skip; not critical
            domain=None,
            capabilities=None,
            presentation=None,
        )

    def _make_stub(self, data: Dict[str, Any]):
        """
        Lightweight DKO stub from stored data.
        Used when full reconstruction fails — provides basic context
        without crashing. DKO will be regenerated on next analysis.
        """
        try:
            from dataset_intelligence.models_v2 import (
                DatasetKnowledgeObject, DatasetIdentity, KnowledgeGraph
            )
            return DatasetKnowledgeObject(
                fingerprint=data.get("fingerprint", ""),
                dataset_name=data.get("dataset_name", "unknown"),
                analyzed_at=data.get("analyzed_at", ""),
                row_count=data.get("row_count", 0),
                column_count=data.get("column_count", 0),
                memory_usage_bytes=data.get("memory_usage_bytes", 0),
                identity=DatasetIdentity(
                    probable_purpose=data.get("identity", {}).get("probable_purpose", ""),
                    primary_entities=data.get("identity", {}).get("primary_entities", []),
                    business_concepts=data.get("identity", {}).get("business_concepts", []),
                ),
                knowledge_graph=KnowledgeGraph(),
                columns={},
            )
        except Exception as e:
            logger.error("[DKO_SERIALIZER] Even stub creation failed: %s", e)
            return None

    def to_jsonb_string(self, dko) -> str:
        """Serialize DKO to a JSON string (for logging/debugging)."""
        return json.dumps(self.serialize(dko), default=str)


# Singleton
dko_serializer = DKOSerializer()

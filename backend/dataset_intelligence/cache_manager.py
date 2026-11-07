"""
Dataset Intelligence Engine — Cache Manager

Manages fingerprint-keyed disk persistence of DatasetKnowledgeObjects.

Caching strategy:
  - Key: schema_fingerprint (SHA-256 hash of column names + dtypes + row count)
  - Storage: JSON files in backend/data/intelligence_cache/
  - Format: Compressed JSON (DKO.to_dict())
  - Memory: In-memory dict for hot cache (avoids disk reads)

Cache hit behavior:
  - Returns DKO instantly (no Python computation, no LLM call)

Cache miss behavior:
  - Returns None (caller must run the full pipeline)
  - After pipeline completes, caller should call save()
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from .models import (
    AnalysisCapability, CapabilitySet, ColumnCorrelation, ColumnRole,
    ColumnSemanticType, DataDomain, DataInsight, DatasetPresentation,
    DomainResult, InsightSeverity, PredictedQuestion, QualityIssue,
    QualityIssueType, QualityReport, NumericStats, CategoricalStats,
    DatetimeStats
)
from .models_v2 import (
    DatasetKnowledgeObject, ColumnIntelligence, DatasetIdentity,
    DatasetObservation, KnowledgeGraph, Dependency, GroupingRelationship,
    AnalyticalGroup, AnalyticalOpportunity, InferenceMetadata,
    IntelligenceStackVersion
)

logger = logging.getLogger(__name__)

# Cache directory: two levels up from this file (/backend/data/intelligence_cache/)
_CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "intelligence_cache"
)
os.makedirs(_CACHE_DIR, exist_ok=True)

# In-memory hot cache: fingerprint → DKO
_HOT_CACHE: Dict[str, DatasetKnowledgeObject] = {}


class CacheManager:
    """
    Manages the DKO cache.
    All methods are safe to call even when the cache directory is missing —
    it will be created automatically.
    """

    def get(self, fingerprint: str) -> Optional[DatasetKnowledgeObject]:
        """
        Retrieve a DKO from cache.
        Checks hot (in-memory) cache first, then disk.

        Returns None on cache miss.
        """
        # Hot cache
        if fingerprint in _HOT_CACHE:
            logger.info(f"CacheManager: hot cache hit for fingerprint '{fingerprint}'")
            return _HOT_CACHE[fingerprint]

        # Disk cache
        path = self._cache_path(fingerprint)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                dko = self._deserialize(data)
                _HOT_CACHE[fingerprint] = dko
                logger.info(f"CacheManager: disk cache hit for fingerprint '{fingerprint}'")
                return dko
            except Exception as e:
                logger.warning(f"CacheManager: failed to deserialize cached DKO '{fingerprint}': {e}")
                # Delete corrupted cache file
                try:
                    os.remove(path)
                except OSError:
                    pass

        return None

    def save(self, dko: DatasetKnowledgeObject) -> None:
        """
        Persist a DKO to the cache.
        Saves to both hot cache and disk.
        """
        fingerprint = dko.fingerprint
        _HOT_CACHE[fingerprint] = dko

        path = self._cache_path(fingerprint)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(dko.to_dict(), f, indent=2, default=str)
            logger.info(f"CacheManager: saved DKO for fingerprint '{fingerprint}' to {path}")
        except Exception as e:
            logger.warning(f"CacheManager: failed to save DKO to disk: {e}")

    def invalidate(self, fingerprint: str) -> None:
        """Remove a DKO from the cache."""
        _HOT_CACHE.pop(fingerprint, None)
        path = self._cache_path(fingerprint)
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"CacheManager: invalidated cache for '{fingerprint}'")
            except OSError as e:
                logger.warning(f"CacheManager: could not remove cache file: {e}")

    def exists(self, fingerprint: str) -> bool:
        """Check whether a DKO exists in cache without loading it."""
        return fingerprint in _HOT_CACHE or os.path.exists(self._cache_path(fingerprint))

    def _cache_path(self, fingerprint: str) -> str:
        return os.path.join(_CACHE_DIR, f"{fingerprint}.json")

    def _deserialize_metadata(self, data: dict) -> InferenceMetadata:
        return InferenceMetadata(
            confidence=data.get("confidence", 1.0),
            importance=data.get("importance", 0.0),
            explanation=data.get("explanation", ""),
            lineage=data.get("lineage", "")
        )

    def _deserialize(self, data: dict) -> DatasetKnowledgeObject:
        """
        Reconstruct a DatasetKnowledgeObject from its JSON representation
        using the new models_v2 architecture.
        """
        # Versions
        v_data = data.get("versions", {})
        versions = IntelligenceStackVersion(
            engine_version=v_data.get("engine_version", "1.0.0"),
            dko_schema_version=v_data.get("dko_schema_version", "2.0.0"),
            knowledge_graph_version=v_data.get("knowledge_graph_version", "1.0.0"),
            semantic_model_version=v_data.get("semantic_model_version", "1.0.0"),
            observation_engine_version=v_data.get("observation_engine_version", "1.0.0")
        )

        # Identity
        i_data = data.get("identity", {})
        identity = DatasetIdentity(
            probable_purpose=i_data.get("probable_purpose", ""),
            primary_entities=i_data.get("primary_entities", []),
            business_concepts=i_data.get("business_concepts", []),
            overall_readiness=i_data.get("overall_readiness", "unknown"),
            kpis=i_data.get("kpis", []),
            strongest_groupings=i_data.get("strongest_groupings", [])
        )

        # Observations
        observations = []
        for o_data in data.get("observations", []):
            observations.append(DatasetObservation(
                observation=o_data.get("observation", ""),
                category=o_data.get("category", ""),
                metadata=self._deserialize_metadata(o_data.get("metadata", {}))
            ))

        # Columns
        columns = {}
        cols_raw = data.get("columns")
        cols_dict = cols_raw if isinstance(cols_raw, dict) else {}
        for col_name, c in cols_dict.items():
            num_data = c.get("numeric_stats")
            cat_data = c.get("categorical_stats")
            dt_data = c.get("datetime_stats")

            numeric_stats = NumericStats(**{k: num_data[k] for k in NumericStats.__dataclass_fields__ if k in num_data}) if num_data else None
            categorical_stats = CategoricalStats(**{k: cat_data[k] for k in CategoricalStats.__dataclass_fields__ if k in cat_data}) if cat_data else None
            datetime_stats = DatetimeStats(**{k: dt_data[k] for k in DatetimeStats.__dataclass_fields__ if k in dt_data}) if dt_data else None

            columns[col_name] = ColumnIntelligence(
                name=c.get("name", ""),
                position=c.get("position", 0),
                dtype_raw=c.get("dtype_raw", ""),
                dtype_category=c.get("dtype_category", "unknown"),
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
                numeric_stats=numeric_stats,
                categorical_stats=categorical_stats,
                datetime_stats=datetime_stats,
                characteristics=c.get("characteristics", []),
                metadata=self._deserialize_metadata(c.get("metadata", {}))
            )

        # Analytical Groups
        analytical_groups = []
        for g_data in data.get("analytical_groups", []):
            analytical_groups.append(AnalyticalGroup(
                name=g_data.get("name", ""),
                group_type=g_data.get("group_type", ""),
                columns=g_data.get("columns", []),
                metadata=self._deserialize_metadata(g_data.get("metadata", {}))
            ))

        # Knowledge Graph
        kg_data = data.get("knowledge_graph", {})
        correlations = []
        for corr in kg_data.get("correlations", []):
            correlations.append(ColumnCorrelation(
                col_a=corr["col_a"], col_b=corr["col_b"],
                pearson_r=corr["pearson_r"], strength=corr["strength"],
                direction=corr["direction"]
            ))
            
        groupings = []
        for g_rel in kg_data.get("groupings", []):
            groupings.append(GroupingRelationship(
                dimension=g_rel.get("dimension", ""),
                metrics=g_rel.get("metrics", []),
                metadata=self._deserialize_metadata(g_rel.get("metadata", {}))
            ))
            
        hierarchies = []
        for h_rel in kg_data.get("hierarchies", []):
            hierarchies.append(Dependency(
                source=h_rel.get("source", ""),
                target=h_rel.get("target", ""),
                relationship_type=h_rel.get("relationship_type", ""),
                metadata=self._deserialize_metadata(h_rel.get("metadata", {}))
            ))

        dependencies = []
        for d_rel in kg_data.get("dependencies", []):
            dependencies.append(Dependency(
                source=d_rel.get("source", ""),
                target=d_rel.get("target", ""),
                relationship_type=d_rel.get("relationship_type", ""),
                metadata=self._deserialize_metadata(d_rel.get("metadata", {}))
            ))
            
        knowledge_graph = KnowledgeGraph(
            correlations=correlations,
            groupings=groupings,
            hierarchies=hierarchies,
            dependencies=dependencies
        )

        # Opportunities
        opportunities = []
        for o_data in data.get("opportunities", []):
            opportunities.append(AnalyticalOpportunity(
                name=o_data.get("name", ""),
                description=o_data.get("description", ""),
                required_columns=o_data.get("required_columns", []),
                metadata=self._deserialize_metadata(o_data.get("metadata", {}))
            ))

        # Legacy structures
        quality_report = None
        if data.get("quality_report"):
            q_data = data["quality_report"]
            issues = []
            for qi in q_data.get("issues", []):
                issues.append(QualityIssue(
                    issue_type=QualityIssueType(qi.get("issue_type", "missing_values")),
                    severity=InsightSeverity(qi.get("severity", "low")),
                    affected_columns=qi.get("affected_columns", []),
                    description=qi.get("description", ""),
                    recommendation=qi.get("recommendation", ""),
                    impact_score=qi.get("impact_score", 0.0),
                ))
            quality_report = QualityReport(
                overall_score=q_data.get("overall_score", 0.0),
                completeness_score=q_data.get("completeness_score", 0.0),
                consistency_score=q_data.get("consistency_score", 0.0),
                uniqueness_score=q_data.get("uniqueness_score", 0.0),
                issues=issues,
                duplicate_row_count=q_data.get("duplicate_row_count", 0),
                duplicate_row_pct=q_data.get("duplicate_row_pct", 0.0),
                total_missing_cells=q_data.get("total_missing_cells", 0),
                total_missing_pct=q_data.get("total_missing_pct", 0.0),
                columns_with_issues=q_data.get("columns_with_issues", []),
                analysis_readiness=q_data.get("analysis_readiness", "good"),
            )

        domain = None
        if data.get("domain"):
            dom_data = data["domain"]
            domain = DomainResult(
                domain=DataDomain(dom_data.get("domain", "General Purpose")),
                confidence=dom_data.get("confidence", 0.0),
                evidence=dom_data.get("evidence", []),
                secondary_domain=DataDomain(dom_data["secondary_domain"]) if dom_data.get("secondary_domain") else None,
            )

        predicted_questions = []
        for pq in data.get("predicted_questions", []):
            predicted_questions.append(PredictedQuestion(
                question=pq.get("question", ""),
                category=pq.get("category", ""),
                confidence=pq.get("confidence", 0.0),
                relevant_columns=pq.get("relevant_columns", []),
                suggested_chart=pq.get("suggested_chart"),
            ))

        insights = []
        for ins in data.get("insights", []):
            insights.append(DataInsight(
                title=ins.get("title", ""),
                description=ins.get("description", ""),
                severity=InsightSeverity(ins.get("severity", "low")),
                category=ins.get("category", ""),
                affected_columns=ins.get("affected_columns", []),
                evidence=ins.get("evidence", {}),
                recommendation=ins.get("recommendation"),
                importance_score=ins.get("importance_score", 0.0),
            ))

        pres_data = data.get("presentation")
        presentation = None
        if pres_data:
            presentation = DatasetPresentation(
                presentation=pres_data.get("presentation", ""),
                version=pres_data.get("version", "1.0"),
                model_used=pres_data.get("model_used", "cached"),
                generated_at=pres_data.get("generated_at", ""),
                generated_by=pres_data.get("generated_by", "cached"),
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
            knowledge_graph=knowledge_graph,
            opportunities=opportunities,
            insights=insights,
            predicted_questions=predicted_questions,
            quality_report=quality_report,
            domain=domain,
            presentation=presentation
        )


cache_manager = CacheManager()

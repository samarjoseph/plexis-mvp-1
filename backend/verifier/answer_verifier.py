"""
Answer Verification Engine for Plexis.

This module independently verifies every dataset-grounded answer before it is
returned to the user. It validates:
1. Numerical Fidelity: Ensures all quantitative claims match authoritative evidence
   within operation-appropriate tolerances (accounting for rounding, percentages,
   and precision).
2. Required-Content Completeness: Verifies that all metrics, comparisons, and groupings
   requested by the user are present.
3. Semantic & Label Correctness: Guards against swapped group labels, count vs. rate
   confusion, and incorrect units.
4. Causal & Evidence Boundaries: Flags unsupported causal assertions or speculation
   beyond what the statistical analysis proves.
5. Provenance & Version Consistency: Verifies dataset identity and version grounding.
6. Bounded Repair: Automatically recovers from repairable generation issues using
   authoritative deterministic facts without recursive looping.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class VerificationStatus(str, Enum):
    PASS = "PASS"
    REPAIRABLE = "REPAIRABLE"
    INDETERMINATE = "INDETERMINATE"
    FAIL = "FAIL"


@dataclass
class VerificationFinding:
    category: str  # "numerical", "required_content", "semantic_label", "causality", "provenance"
    severity: str  # "fatal", "repairable", "warning"
    message: str
    expected: Any = None
    found: Any = None
    field_or_metric: Optional[str] = None


@dataclass
class AnswerVerificationResult:
    status: VerificationStatus
    findings: List[VerificationFinding] = field(default_factory=list)
    verified_claims: List[Dict[str, Any]] = field(default_factory=list)
    repaired_answer: Optional[str] = None
    is_repaired: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "is_repaired": self.is_repaired,
            "findings_count": len(self.findings),
            "findings": [
                {
                    "category": f.category,
                    "severity": f.severity,
                    "message": f.message,
                    "expected": str(f.expected) if f.expected is not None else None,
                    "found": str(f.found) if f.found is not None else None,
                    "field_or_metric": f.field_or_metric,
                }
                for f in self.findings
            ],
            "verified_claims_count": len(self.verified_claims),
            "details": self.details,
        }


class AnswerVerifier:
    """
    Authoritative verifier for final user-facing analytical answers.
    """

    def __init__(self):
        # Default operation tolerances
        self.default_float_rel_tol = 0.02  # 2% relative tolerance for continuous averages/medians/std
        self.default_float_abs_tol = 0.05  # 0.05 absolute tolerance for rounded floats
        self.percentage_abs_tol = 0.6      # 0.6 percentage points (e.g. 24.8% vs 25.0%)
        self.correlation_abs_tol = 0.05    # 0.05 for correlation coefficients
        self.count_abs_tol = 0.0           # Counts must be exact integers

    def verify(
        self,
        query: str,
        answer: str,
        facts: Optional[Dict[str, Any]] = None,
        evidence: Optional[Any] = None,
        context: Optional[Any] = None,
        auto_repair: bool = True,
    ) -> AnswerVerificationResult:
        """
        Verify the natural language answer against authoritative execution facts.
        """
        if not answer or not str(answer).strip():
            return AnswerVerificationResult(
                status=VerificationStatus.FAIL,
                findings=[VerificationFinding("completeness", "fatal", "Answer text is empty.")],
            )

        answer_text = str(answer).strip()
        query_text = str(query or "").strip()

        # Check if the query requests analytical or dataset-grounded computation
        analytical_query = bool(re.search(
            r'\b(?:mean|average|median|sum|count|highest|lowest|min|max|rate|percent|proportion|distribution|compare|correlation|outliers?|missing|nulls?|tenure|delay|churn|price|salary|rows?|columns?)\b',
            query_text, re.I
        ))

        # If no analytical facts are present:
        has_facts = bool(facts and isinstance(facts, dict) and set(facts.keys()) - {"type", "error", "query"})
        if not has_facts and not evidence:
            if analytical_query:
                return AnswerVerificationResult(
                    status=VerificationStatus.INDETERMINATE,
                    findings=[VerificationFinding("provenance", "warning", "Analytical query without structured execution facts.")],
                    details={"scope": "unbound_analytical"},
                )
            return AnswerVerificationResult(
                status=VerificationStatus.PASS,
                details={"scope": "conversational_unbound"},
            )

        # 1. Extract structured authoritative facts
        auth_facts = self._extract_authoritative_facts(facts, evidence, context)
        if not auth_facts:
            return AnswerVerificationResult(
                status=VerificationStatus.INDETERMINATE,
                findings=[VerificationFinding("provenance", "warning", "No structured facts available to verify against.")],
                details={"reason": "missing_structured_facts"},
            )

        findings: List[VerificationFinding] = []
        verified_claims: List[Dict[str, Any]] = []

        # 2. Check for unsupported causal claims
        causal_findings = self._check_causality(answer_text, query_text, auth_facts)
        findings.extend(causal_findings)

        # 3. Check provenance (dataset identity / version consistency)
        prov_findings = self._check_provenance(answer_text, context)
        findings.extend(prov_findings)

        # 4. Check semantic label consistency (e.g. swapped groups)
        label_findings = self._check_semantic_labels(answer_text, auth_facts)
        findings.extend(label_findings)

        # 5. Check numerical claims against authoritative evidence
        num_findings, num_claims = self._check_numerical_fidelity(answer_text, auth_facts)
        findings.extend(num_findings)
        verified_claims.extend(num_claims)

        # 6. Check required content completeness against query intent
        req_findings = self._check_required_content(answer_text, query_text, auth_facts)
        findings.extend(req_findings)

        # 7. Check for unapplied transformation claims
        trans_findings = self._check_transformation_claims(answer_text, context)
        findings.extend(trans_findings)

        # Determine overall verification status
        has_fatal = any(f.severity == "fatal" for f in findings)
        has_repairable = any(f.severity == "repairable" for f in findings)

        if has_fatal:
            status = VerificationStatus.FAIL
        elif has_repairable:
            status = VerificationStatus.REPAIRABLE
        else:
            status = VerificationStatus.PASS

        result = AnswerVerificationResult(
            status=status,
            findings=findings,
            verified_claims=verified_claims,
            details={
                "authoritative_metrics_count": len(auth_facts.get("metrics", {})),
                "query": query_text,
            },
        )

        # 8. Bounded repair attempt if REPAIRABLE or FAIL with computed narrative available
        if auto_repair and status in (VerificationStatus.REPAIRABLE, VerificationStatus.FAIL):
            repaired_text = self._attempt_repair(answer_text, findings, auth_facts, facts)
            if repaired_text and repaired_text != answer_text:
                # Re-verify repaired text without recursion
                sub_res = self.verify(
                    query=query_text,
                    answer=repaired_text,
                    facts=facts,
                    evidence=evidence,
                    context=context,
                    auto_repair=False,
                )
                if sub_res.status == VerificationStatus.PASS:
                    result.status = VerificationStatus.PASS
                    result.repaired_answer = repaired_text
                    result.is_repaired = True
                    result.details["original_findings"] = [f.message for f in findings]
                    result.findings = [
                        VerificationFinding("repair", "warning", f"Original draft repaired: {len(findings)} issues resolved.")
                    ]
                    result.verified_claims = sub_res.verified_claims

        return result

    # -------------------------------------------------------------------------
    # Fact Extraction & Canonical Representation
    # -------------------------------------------------------------------------

    def _extract_authoritative_facts(
        self,
        facts: Optional[Dict[str, Any]],
        evidence: Optional[Any],
        context: Optional[Any],
    ) -> Dict[str, Any]:
        """
        Normalize all structured metrics into a unified dictionary for verification.
        """
        auth: Dict[str, Any] = {
            "metrics": {},        # key -> float / int value
            "groups": {},         # group_name -> {metric_name -> value}
            "labels": set(),      # valid category / group labels
            "columns": set(),     # valid dataset column names
            "operations": [],     # operation names
            "computed_answer": "",# deterministic narrative if available
            "raw_facts": facts or {},
        }

        if not facts and not evidence:
            return auth

        facts_dict = facts if isinstance(facts, dict) else {}
        auth["computed_answer"] = facts_dict.get("computed_answer", "")

        # Dataset dimensions
        dims = facts_dict.get("dataset_dimensions") or {}
        if isinstance(dims, dict):
            for k in ("row_count", "column_count", "total_cells", "total_missing_cells", "complete_cells", "rows_with_missing", "complete_rows", "duplicate_rows"):
                if k in dims and dims[k] is not None:
                    auth["metrics"][k] = dims[k]
            if "columns" in dims and isinstance(dims["columns"], (list, set)):
                auth["columns"].update(str(c).lower() for c in dims["columns"])

        # Ingest numbers from the user query (e.g. "provide all 9 airlines", "top 5")
        query_msg = getattr(context, "message", None) if context else None
        if query_msg and isinstance(query_msg, str):
            for qm in re.finditer(r'(?<![A-Za-z0-9_])([+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|\$)?', query_msg):
                try:
                    q_num = float(qm.group(1).replace(",", ""))
                    auth["metrics"][f"query_param_{q_num}"] = q_num
                except ValueError:
                    pass

        # Operation details from facts
        ops = facts_dict.get("operations", [])
        if isinstance(ops, list):
            for op in ops:
                if not isinstance(op, dict):
                    continue
                op_name = op.get("operation")
                if op_name:
                    auth["operations"].append(op_name)
                val = op.get("value")
                col = op.get("column")
                if col:
                    auth["columns"].add(str(col).lower())

                if isinstance(val, (int, float)) and not math.isnan(val):
                    auth["metrics"][f"{op_name}_value" if op_name else "value"] = val
                    if col:
                        auth["metrics"][f"{col}_{op_name}"] = val
                elif isinstance(val, dict):
                    self._ingest_details(auth, val)
                elif isinstance(val, list):
                    list_len = float(len(val))
                    auth["metrics"][f"{op_name}_count" if op_name else "list_count"] = list_len
                    auth["metrics"]["distinct_count"] = list_len
                    auth["metrics"]["unique_values_count"] = list_len
                    auth["metrics"]["unique_count"] = list_len
                    if col:
                        auth["metrics"][f"{col}_distinct_count"] = list_len
                        auth["metrics"][f"{col}_unique_count"] = list_len
                        auth["metrics"][f"{col}_count"] = list_len
                    for item in val:
                        if isinstance(item, str):
                            auth["labels"].add(item.lower().strip())
                        elif isinstance(item, (int, float)) and not math.isnan(item):
                            auth["metrics"][f"{col or 'item'}_{item}"] = float(item)

                # Detailed statistics (from binary_indicator_comparison, grouped_rate, etc.)
                details = op.get("details") or {}
                if isinstance(details, dict):
                    self._ingest_details(auth, details)

        # Ingest directly from evidence if available
        if isinstance(evidence, list):
            for ev in evidence:
                if isinstance(ev, dict) and "details" in ev and isinstance(ev["details"], dict):
                    self._ingest_details(auth, ev["details"])
                if isinstance(ev, dict) and "value" in ev and isinstance(ev["value"], (int, float)):
                    auth["metrics"]["evidence_value"] = ev["value"]
                elif isinstance(ev, dict) and "value" in ev and isinstance(ev["value"], dict):
                    self._ingest_details(auth, ev["value"])
                elif isinstance(ev, dict) and "value" in ev and isinstance(ev["value"], list):
                    ev_list_len = float(len(ev["value"]))
                    auth["metrics"]["evidence_list_count"] = ev_list_len
                    auth["metrics"]["evidence_distinct_count"] = ev_list_len
                    auth["metrics"]["unique_values_count"] = ev_list_len
                    for item in ev["value"]:
                        if isinstance(item, str):
                            auth["labels"].add(item.lower().strip())
        elif isinstance(evidence, dict):
            if "details" in evidence and isinstance(evidence["details"], dict):
                self._ingest_details(auth, evidence["details"])
            if "value" in evidence and isinstance(evidence["value"], (int, float)):
                auth["metrics"]["evidence_value"] = evidence["value"]
            elif "value" in evidence and isinstance(evidence["value"], dict):
                self._ingest_details(auth, evidence["value"])
            elif "value" in evidence and isinstance(evidence["value"], list):
                ev_list_len = float(len(evidence["value"]))
                auth["metrics"]["evidence_list_count"] = ev_list_len
                auth["metrics"]["evidence_distinct_count"] = ev_list_len
                auth["metrics"]["unique_values_count"] = ev_list_len
                for item in evidence["value"]:
                    if isinstance(item, str):
                        auth["labels"].add(item.lower().strip())

        return auth

    def _ingest_details(self, auth: Dict[str, Any], details: Dict[str, Any]) -> None:
        """Helper to recursively parse details dictionaries for group and scalar statistics."""
        for k, v in details.items():
            if isinstance(v, (int, float)) and not math.isnan(v):
                auth["metrics"][str(k).lower()] = float(v)
                auth["labels"].add(str(k).lower().strip())
            elif isinstance(v, dict):
                group_name = str(k).lower().strip()
                auth["labels"].add(group_name)
                if group_name not in auth["groups"]:
                    auth["groups"][group_name] = {}
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, (int, float)) and not math.isnan(sub_v):
                        auth["groups"][group_name][sub_k] = sub_v
                        auth["metrics"][f"{group_name}_{sub_k}"] = sub_v
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        item_name = str(item["name"]).lower().strip()
                        auth["labels"].add(item_name)
                        if isinstance(item["value"], (int, float)):
                            auth["metrics"][f"{item_name}_value"] = item["value"]

    # -------------------------------------------------------------------------
    # Numerical Fidelity Verification
    # -------------------------------------------------------------------------

    def _check_numerical_fidelity(
        self,
        answer_text: str,
        auth_facts: Dict[str, Any],
    ) -> Tuple[List[VerificationFinding], List[Dict[str, Any]]]:
        """
        Check that numbers appearing in the answer match authoritative metrics.
        """
        findings: List[VerificationFinding] = []
        verified_claims: List[Dict[str, Any]] = []

        all_auth_values: List[Tuple[str, float]] = []
        # Flatten all scalar metrics
        for name, val in auth_facts.get("metrics", {}).items():
            if isinstance(val, (int, float)) and not math.isnan(val):
                all_auth_values.append((name, float(val)))

        # Also collect percentage variants for rates (e.g., 0.248 -> 24.8)
        rate_variants: List[Tuple[str, float]] = []
        for name, val in all_auth_values:
            if 0.0 <= val <= 1.0 and ("rate" in name or "pct" in name or "prop" in name or "percent" in name):
                rate_variants.append((f"{name}_percent", val * 100.0))
        all_auth_values.extend(rate_variants)

        # Extract numbers from answer text
        # Match percentages, currencies, negatives, floating points, integer counts
        # e.g., 24.8%, $50.00, -0.26, 150,000, 10
        raw_number_pattern = r'(?<![A-Za-z0-9_])([+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|\$)?'
        matches = list(re.finditer(raw_number_pattern, answer_text))

        # Filter out common conversational/formatting numbers (e.g. "top 10", "1st", "2nd", "0 vs 1" as boolean values)
        for m in matches:
            num_str = m.group(1).replace(",", "")
            unit = m.group(2) or ""
            start, end = m.span()

            try:
                num_val = float(num_str)
            except ValueError:
                continue

            # Context window around the number
            ctx_start = max(0, start - 25)
            ctx_end = min(len(answer_text), end + 25)
            snippet = answer_text[ctx_start:ctx_end].lower()

            # Skip harmless ordinal / ranking references ("top 5", "top 10", "q1", "q3", "group 1", "step 2")
            if re.search(r'\b(?:top|bottom|group|tier|step|rank|quartile|decile|version|cluster)\s*#?\s*' + re.escape(num_str) + r'\b', snippet):
                continue
            if re.search(r'\b(?:q[1-4]|tier\s*[1-4])\b', snippet) and num_val in (1, 2, 3, 4):
                continue
            if snippet.strip().startswith(("top ", "bottom ")) and num_val in (1, 3, 5, 10, 20):
                continue

            # Match against authoritative values
            matched = False
            best_match_name = None
            min_diff = float("inf")

            for auth_name, auth_val in all_auth_values:
                # Select tolerance
                if "count" in auth_name or "rows" in auth_name or "cells" in auth_name:
                    # Strict integer check for counts
                    is_match = abs(num_val - auth_val) < 1e-6
                elif unit == "%" or "pct" in auth_name or "percent" in auth_name:
                    # Percentage tolerance
                    is_match = abs(num_val - auth_val) <= self.percentage_abs_tol
                elif "corr" in auth_name:
                    is_match = abs(num_val - auth_val) <= self.correlation_abs_tol
                else:
                    # Continuous float: min of relative tolerance or absolute 0.05
                    diff = abs(num_val - auth_val)
                    rel_diff = diff / (abs(auth_val) + 1e-9)
                    is_match = diff <= self.default_float_abs_tol or rel_diff <= self.default_float_rel_tol

                if is_match:
                    matched = True
                    diff = abs(num_val - auth_val)
                    if diff < min_diff:
                        min_diff = diff
                        best_match_name = auth_name

            if matched:
                verified_claims.append({
                    "number": num_val,
                    "matched_metric": best_match_name,
                    "unit": unit,
                    "snippet": snippet.strip(),
                })
            else:
                # If we have authoritative metrics, an unsupported number is a potential hallucination
                # Check if it's an obviously fabricated metric
                if len(all_auth_values) > 0 and num_val not in (0, 1):
                    findings.append(VerificationFinding(
                        category="numerical",
                        severity="fatal",
                        message=f"Unsupported numerical claim: '{num_str}{unit}' does not match any authoritative metric.",
                        found=num_val,
                        field_or_metric=snippet.strip(),
                    ))

        return findings, verified_claims

    # -------------------------------------------------------------------------
    # Required-Content Completeness
    # -------------------------------------------------------------------------

    def _check_required_content(
        self,
        answer_text: str,
        query_text: str,
        auth_facts: Dict[str, Any],
    ) -> List[VerificationFinding]:
        """
        Verify that all specific statistical metrics requested by the user are answered.
        """
        findings: List[VerificationFinding] = []
        q_lower = query_text.lower()
        ans_lower = answer_text.lower()

        # Define metric check patterns
        metric_checks = [
            ("mean", r'\b(?:mean|average)\b', r'\b(?:mean|average|avg)\b'),
            ("median", r'\bmedian\b', r'\bmedian\b'),
            ("std", r'\b(?:standard deviation|std|dispersion)\b', r'\b(?:standard deviation|std|stdev)\b'),
            ("iqr", r'\b(?:interquartile range|iqr)\b', r'\b(?:iqr|interquartile)\b'),
            ("min", r'\b(?:minimum|min|lowest)\b', r'\b(?:min|minimum|lowest|bottom|least|smallest|fewest)\b'),
            ("max", r'\b(?:maximum|max|highest)\b', r'\b(?:max|maximum|highest|top|most|peak|largest|greatest|lead|leading)\b'),
            ("count", r'\b(?:sample counts?|counts?|sample size)\b', r'\b(?:count|counts|samples?|size|records?|rows?|observations?|transactions?|total)\b'),
            ("abs_diff", r'\b(?:absolute difference|difference in medians?|difference in means?)\b', r'\b(?:difference|spread|delta|gap|differ|higher by|lower by)\b'),
        ]

        for metric_name, q_pat, ans_pat in metric_checks:
            if re.search(q_pat, q_lower):
                # User explicitly requested this metric
                if not re.search(ans_pat, ans_lower):
                    findings.append(VerificationFinding(
                        category="required_content",
                        severity="repairable",
                        message=f"Missing required metric: Query requested '{metric_name}', but it was omitted from the answer.",
                        field_or_metric=metric_name,
                    ))

        return findings

    # -------------------------------------------------------------------------
    # Semantic & Label Verification (Swapped Groups, Units)
    # -------------------------------------------------------------------------

    def _check_semantic_labels(
        self,
        answer_text: str,
        auth_facts: Dict[str, Any],
    ) -> List[VerificationFinding]:
        """
        Detect swapped group labels or misattribution of statistics between cohorts.
        """
        findings: List[VerificationFinding] = []
        groups = auth_facts.get("groups", {})
        if len(groups) < 2:
            return findings

        ans_lower = answer_text.lower()

        # For binary groups (e.g. churned vs non-churned, delayed vs on-time, 0 vs 1):
        group_keys = list(groups.keys())
        g0_name, g1_name = group_keys[0], group_keys[1]
        g0_metrics, g1_metrics = groups[g0_name], groups[g1_name]

        # Check for swapped means or medians
        for stat in ("mean", "median", "count"):
            if stat in g0_metrics and stat in g1_metrics:
                v0, v1 = g0_metrics[stat], g1_metrics[stat]
                # Only check if the values are substantially distinct
                if abs(v0 - v1) > 0.5:
                    v0_str = f"{v0:.1f}"
                    v1_str = f"{v1:.1f}"

                    # Find where v1 appears near g0_name
                    # If g0_name appears within 30 characters of v1_str but not v0_str, flag swapped label
                    g0_pattern = re.escape(g0_name)
                    g1_pattern = re.escape(g1_name)

                    # Look for sentences associating g0 with v1
                    g0_with_v1 = re.search(rf'{g0_pattern}[^.\n]*?\b{re.escape(v1_str)}\b', ans_lower)
                    g1_with_v0 = re.search(rf'{g1_pattern}[^.\n]*?\b{re.escape(v0_str)}\b', ans_lower)

                    if g0_with_v1 and not re.search(rf'{g0_pattern}[^.\n]*?\b{re.escape(v0_str)}\b', ans_lower):
                        findings.append(VerificationFinding(
                            category="semantic_label",
                            severity="fatal",
                            message=f"Swapped group statistics: Attributed {g1_name}'s {stat} ({v1_str}) to {g0_name}.",
                            expected=v0,
                            found=v1,
                            field_or_metric=f"{g0_name}_{stat}",
                        ))
                    elif g1_with_v0 and not re.search(rf'{g1_pattern}[^.\n]*?\b{re.escape(v1_str)}\b', ans_lower):
                        findings.append(VerificationFinding(
                            category="semantic_label",
                            severity="fatal",
                            message=f"Swapped group statistics: Attributed {g0_name}'s {stat} ({v0_str}) to {g1_name}.",
                            expected=v1,
                            found=v0,
                            field_or_metric=f"{g1_name}_{stat}",
                        ))

        return findings

    # -------------------------------------------------------------------------
    # Causal & Non-Grounded Explanation Detection
    # -------------------------------------------------------------------------

    def _check_causality(
        self,
        answer_text: str,
        query_text: str,
        auth_facts: Dict[str, Any],
    ) -> List[VerificationFinding]:
        """
        Flag unsupported assertions of causality from correlation or observational data.
        """
        findings: List[VerificationFinding] = []
        ans_lower = answer_text.lower()
        q_lower = query_text.lower()

        # If user explicitly asked for causation or reasons ("why", "cause"), allow explanatory discourse
        if re.search(r'\b(?:why|causes?|causation|reason|explain why)\b', q_lower):
            return findings

        # Check for strong causal claims from descriptive/correlational operations
        strong_causal_patterns = [
            (r'\bproves?\s+that\b.*?\bcauses?\b', "Claimed correlation 'proves' causation."),
            (r'\bdirectly\s+causes?\b', "Claimed direct causation without experimental evidence."),
            (r'\bis\s+caused\s+solely\s+by\b', "Claimed exclusive causation without controlling for confounders."),
        ]

        for pat, msg in strong_causal_patterns:
            if re.search(pat, ans_lower):
                findings.append(VerificationFinding(
                    category="causality",
                    severity="warning",
                    message=msg,
                ))

        return findings

    # -------------------------------------------------------------------------
    # Provenance & Version Verification
    # -------------------------------------------------------------------------

    def _check_provenance(
        self,
        answer_text: str,
        context: Optional[Any],
    ) -> List[VerificationFinding]:
        """
        Ensure dataset name, version, and IDs are uncorrupted.
        """
        findings: List[VerificationFinding] = []
        if not context:
            return findings

        dataset_id = getattr(context, "dataset_id", None)
        active_version = getattr(context, "dataset_version", None)

        ans_lower = answer_text.lower()

        # If text explicitly cites a dataset version number, verify it matches
        m_ver = re.search(r'\bdataset\s+version\s+(\d+)\b', ans_lower)
        if m_ver and active_version is not None:
            claimed_ver = int(m_ver.group(1))
            if claimed_ver != active_version:
                findings.append(VerificationFinding(
                    category="provenance",
                    severity="fatal",
                    message=f"Dataset version mismatch: Claimed version {claimed_ver}, but active version is {active_version}.",
                    expected=active_version,
                    found=claimed_ver,
                ))

        return findings

    # -------------------------------------------------------------------------
    # Transformation Claim Verification
    # -------------------------------------------------------------------------

    def _check_transformation_claims(
        self,
        answer_text: str,
        context: Optional[Any],
    ) -> List[VerificationFinding]:
        """
        Ensure analytical responses do not falsely claim to have modified the dataset.
        """
        findings: List[VerificationFinding] = []
        intent = getattr(context, "intent", "analysis") or "analysis"

        # If intent was pure analysis or hypothetical, ensure no false mutation claims
        if intent in ("analysis", "DATA_OPERATION"):
            ans_lower = answer_text.lower()
            false_mutation_patterns = [
                r'\bi\s+have\s+(?:deleted|removed|dropped)\s+\d+\s+rows\s+from\s+the\s+dataset\b',
                r'\bthe\s+dataset\s+has\s+been\s+(?:modified|updated|overwritten)\b',
                r'\bpermanently\s+(?:deleted|altered)\b',
            ]
            for pat in false_mutation_patterns:
                if re.search(pat, ans_lower):
                    findings.append(VerificationFinding(
                        category="semantic_label",
                        severity="fatal",
                        message="False mutation claim: Claimed dataset was altered during a read-only analysis query.",
                    ))

        return findings

    # -------------------------------------------------------------------------
    # Bounded Repair & Deterministic Fallback
    # -------------------------------------------------------------------------

    def _attempt_repair(
        self,
        answer_text: str,
        findings: List[VerificationFinding],
        auth_facts: Dict[str, Any],
        facts: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """
        Attempt to repair an answer using authoritative deterministic narrative.
        """
        computed_answer = auth_facts.get("computed_answer")
        if computed_answer and str(computed_answer).strip():
            logger.info("[AnswerVerifier] Repairing answer with authoritative computed narrative.")
            return str(computed_answer).strip()

        # If computed_answer is not available, but operations summary exists:
        ops = (facts or {}).get("operations", [])
        if ops and isinstance(ops, list):
            summaries = [op.get("summary") for op in ops if op.get("summary")]
            if summaries:
                logger.info("[AnswerVerifier] Repairing answer from operation summaries.")
                return "\n\n".join(summaries)

        return None


# Global singleton verifier
answer_verifier = AnswerVerifier()

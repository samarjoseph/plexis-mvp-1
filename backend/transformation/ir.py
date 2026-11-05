"""
Transformation IR — Intermediate Representation for composable transformations.

This module defines the structured data types that sit between natural language
(user intent) and deterministic pandas execution. The LLM produces IR, the
validator checks it, the compiler executes it, and the result feeds into the
existing TransformationPlan contract.

SAFETY: No eval/exec. Every expression node is explicitly allowlisted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Expression AST
# ──────────────────────────────────────────────────────────────────────────────

# Allowlisted expression node types
ALLOWED_EXPR_TYPES = frozenset({
    'COLUMN',
    'LITERAL',
    'COMPARE',
    'ARITHMETIC',
    'STRING_FN',
    'CASE',
    'COALESCE',
    'NOT',
    'AND',
    'OR',
    'IS_NULL',
    'IS_NOT_NULL',
})

# Allowlisted comparison operators
ALLOWED_COMPARE_OPS = frozenset({
    '==', '!=', '<', '>', '<=', '>=',
    'is_null', 'is_not_null', 'isnull', 'notnull', 'is null', 'is not null',
    'in', 'not_in', 'not in',
})

# Allowlisted arithmetic operators
ALLOWED_ARITHMETIC_OPS = frozenset({'add', 'subtract', 'multiply', 'divide', 'modulo', 'abs'})

# Allowlisted string functions
ALLOWED_STRING_FNS = frozenset({
    'strip', 'lower', 'upper', 'title', 'replace', 'contains',
    'startswith', 'endswith', 'len', 'slice', 'lstrip', 'rstrip',
})

# Maximum expression nesting depth (prevents abuse/stack overflow)
MAX_EXPR_DEPTH = 20


def validate_expr_node(node: dict, depth: int = 0) -> List[str]:
    """
    Validate a single expression node recursively.

    Returns a list of error strings. Empty list = valid.
    """
    errors: List[str] = []

    if not isinstance(node, dict):
        return [f"Expression node must be a dict, got {type(node).__name__}"]

    if depth > MAX_EXPR_DEPTH:
        return [f"Expression depth exceeds maximum ({MAX_EXPR_DEPTH})"]

    node_type = node.get('type', '')
    if node_type not in ALLOWED_EXPR_TYPES:
        errors.append(f"Unknown expression type: '{node_type}'. Allowed: {sorted(ALLOWED_EXPR_TYPES)}")
        return errors

    if node_type == 'COLUMN':
        if not node.get('name'):
            errors.append("COLUMN expression requires 'name'")

    elif node_type == 'LITERAL':
        if 'value' not in node:
            errors.append("LITERAL expression requires 'value'")
        typed = node.get('typed_literal')
        if typed is not None:
            if not isinstance(typed, dict) or typed.get('__typed_literal__') is not True:
                errors.append("LITERAL typed_literal must be a serialized TypedLiteral")
            elif typed.get('value_type') not in {'integer', 'float', 'string', 'boolean', 'null', 'date'}:
                errors.append("LITERAL typed_literal has an unsupported value_type")
            elif 'value' not in typed:
                errors.append("LITERAL typed_literal requires 'value'")

    elif node_type == 'COMPARE':
        op = node.get('operator', '')
        if op not in ALLOWED_COMPARE_OPS:
            errors.append(f"Invalid comparison operator: '{op}'. Allowed: {sorted(ALLOWED_COMPARE_OPS)}")
        for side in ('left', 'right'):
            child = node.get(side)
            if child is None:
                if side == 'right' and op in ('is_null', 'is_not_null', 'isnull', 'notnull', 'is null', 'is not null'):
                    continue
                errors.append(f"COMPARE requires '{side}' operand")
            elif isinstance(child, dict):
                errors.extend(validate_expr_node(child, depth + 1))

    elif node_type in ('IS_NULL', 'IS_NOT_NULL'):
        operand = node.get('operand') or node.get('left')
        if operand is None:
            errors.append(f"{node_type} requires 'operand' or 'left'")
        elif isinstance(operand, dict):
            errors.extend(validate_expr_node(operand, depth + 1))

    elif node_type == 'ARITHMETIC':
        op = node.get('operator', '')
        if op not in ALLOWED_ARITHMETIC_OPS:
            errors.append(f"Invalid arithmetic operator: '{op}'. Allowed: {sorted(ALLOWED_ARITHMETIC_OPS)}")
        if op == 'abs':
            operand = node.get('operand')
            if operand is None:
                errors.append("ARITHMETIC 'abs' requires 'operand'")
            elif isinstance(operand, dict):
                errors.extend(validate_expr_node(operand, depth + 1))
        else:
            for side in ('left', 'right'):
                child = node.get(side)
                if child is None:
                    errors.append(f"ARITHMETIC requires '{side}' operand")
                elif isinstance(child, dict):
                    errors.extend(validate_expr_node(child, depth + 1))

    elif node_type == 'STRING_FN':
        fn = node.get('function', '')
        if fn not in ALLOWED_STRING_FNS:
            errors.append(f"Invalid string function: '{fn}'. Allowed: {sorted(ALLOWED_STRING_FNS)}")
        operand = node.get('operand')
        if operand is None:
            errors.append("STRING_FN requires 'operand'")
        elif isinstance(operand, dict):
            errors.extend(validate_expr_node(operand, depth + 1))

    elif node_type == 'CASE':
        conditions = node.get('conditions', [])
        if not conditions:
            errors.append("CASE requires at least one condition")
        for i, cond in enumerate(conditions):
            when_expr = cond.get('when')
            if when_expr is None:
                errors.append(f"CASE condition[{i}] requires 'when'")
            elif isinstance(when_expr, dict):
                errors.extend(validate_expr_node(when_expr, depth + 1))
            if 'value' not in cond and 'then' not in cond:
                errors.append(f"CASE condition[{i}] requires 'value' or 'then'")
            then_expr = cond.get('then')
            if isinstance(then_expr, dict):
                errors.extend(validate_expr_node(then_expr, depth + 1))

    elif node_type == 'COALESCE':
        exprs = node.get('expressions', [])
        if len(exprs) < 2:
            errors.append("COALESCE requires at least 2 expressions")
        for i, expr in enumerate(exprs):
            if isinstance(expr, dict):
                errors.extend(validate_expr_node(expr, depth + 1))

    elif node_type == 'NOT':
        operand = node.get('operand')
        if operand is None:
            errors.append("NOT requires 'operand'")
        elif isinstance(operand, dict):
            errors.extend(validate_expr_node(operand, depth + 1))

    elif node_type in ('AND', 'OR'):
        for side in ('left', 'right'):
            child = node.get(side)
            if child is None:
                errors.append(f"{node_type} requires '{side}' operand")
            elif isinstance(child, dict):
                errors.extend(validate_expr_node(child, depth + 1))

    return errors


def collect_column_refs(node: dict) -> set:
    """Extract all column names referenced in an expression tree."""
    refs = set()
    if not isinstance(node, dict):
        return refs

    if node.get('type') == 'COLUMN':
        name = node.get('name')
        if name:
            refs.add(name)

    # Recurse into children
    for key in ('left', 'right', 'operand'):
        child = node.get(key)
        if isinstance(child, dict):
            refs |= collect_column_refs(child)

    for cond in node.get('conditions', []):
        if isinstance(cond, dict):
            for k in ('when', 'then'):
                child = cond.get(k)
                if isinstance(child, dict):
                    refs |= collect_column_refs(child)

    for expr in node.get('expressions', []):
        if isinstance(expr, dict):
            refs |= collect_column_refs(expr)

    return refs


# ──────────────────────────────────────────────────────────────────────────────
# Transformation Step
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TransformStep:
    """A single atomic transformation step in the IR."""
    id: str                          # Unique step ID, e.g. "step_1"
    operation: str                   # Primitive name, e.g. "CREATE_COLUMN"
    params: Dict[str, Any]           # Operation-specific parameters
    expression: Optional[Dict] = None  # Expression AST (for computed values)
    depends_on: List[str] = field(default_factory=list)
    label: Optional[str] = None      # Human-readable description

    def to_dict(self) -> dict:
        d = {
            'id': self.id,
            'operation': self.operation,
            'params': self.params,
        }
        if self.expression is not None:
            d['expression'] = self.expression
        if self.depends_on:
            d['depends_on'] = self.depends_on
        if self.label:
            d['label'] = self.label
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'TransformStep':
        return cls(
            id=d.get('id', ''),
            operation=d.get('operation', ''),
            params=d.get('params', {}),
            expression=d.get('expression'),
            depends_on=d.get('depends_on', []),
            label=d.get('label'),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Transformation IR (top level)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TransformationIR:
    """
    The complete Transformation Intermediate Representation.

    Produced by the LLM planner or fast-path wrapper.
    Consumed by the validator → compiler → simulator pipeline.
    """
    goal: str                        # Human-readable goal
    steps: List[TransformStep]       # Ordered steps
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'goal': self.goal,
            'steps': [s.to_dict() for s in self.steps],
            'metadata': self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'TransformationIR':
        steps = [TransformStep.from_dict(s) for s in d.get('steps', [])]
        return cls(
            goal=d.get('goal', ''),
            steps=steps,
            metadata=d.get('metadata', {}),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Primitive Registry
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PrimitiveSpec:
    """Describes a safe execution primitive."""
    name: str
    description: str
    required_params: List[str]
    optional_params: List[str] = field(default_factory=list)
    requires_expression: bool = False
    modifies_shape: bool = False    # True if it changes row/column count

    def to_prompt_spec(self) -> str:
        """Format for inclusion in LLM prompt."""
        parts = [f"  {self.name}: {self.description}"]
        if self.required_params:
            parts.append(f"    Required: {', '.join(self.required_params)}")
        if self.optional_params:
            parts.append(f"    Optional: {', '.join(self.optional_params)}")
        if self.requires_expression:
            parts.append("    Requires: expression (AST)")
        return '\n'.join(parts)


# The canonical primitive registry
PRIMITIVE_REGISTRY: Dict[str, PrimitiveSpec] = {}


def _register(spec: PrimitiveSpec) -> None:
    PRIMITIVE_REGISTRY[spec.name] = spec


# ── Existing operations (map to current 8) ──────────────────────────────────
_register(PrimitiveSpec('FILL', 'Fill missing values in columns',
                        required_params=['columns', 'value']))
_register(PrimitiveSpec('DROP_DUPLICATES', 'Remove duplicate rows',
                        required_params=[], optional_params=['columns']))
_register(PrimitiveSpec('RENAME_COLUMN', 'Rename a column',
                        required_params=['column', 'new_name']))
_register(PrimitiveSpec('DROP_COLUMN', 'Remove a column',
                        required_params=['column'], modifies_shape=True))
_register(PrimitiveSpec('FILTER_ROWS', 'Keep or remove rows matching a condition',
                        required_params=['action'],
                        optional_params=['column', 'operator', 'value'],
                        requires_expression=False,  # can use expr OR params
                        modifies_shape=True))
_register(PrimitiveSpec('REMOVE_EMPTY_ROWS', 'Remove rows containing missing, null, NaN, or empty values',
                        required_params=[],
                        optional_params=['how', 'columns'],
                        modifies_shape=True))
_register(PrimitiveSpec('CAST', 'Convert column data type',
                        required_params=['column', 'dtype']))
_register(PrimitiveSpec('REPLACE', 'Replace values in a column',
                        required_params=['column', 'old_value', 'new_value']))
_register(PrimitiveSpec('SORT_ROWS', 'Sort rows by a column',
                        required_params=['column', 'ascending']))

# ── New composable primitives ────────────────────────────────────────────────
_register(PrimitiveSpec('CREATE_COLUMN', 'Create a new column from an expression',
                        required_params=['column'],
                        requires_expression=True, modifies_shape=True))
_register(PrimitiveSpec('MODIFY_COLUMN', 'Modify an existing column using an expression',
                        required_params=['column'],
                        requires_expression=True))
_register(PrimitiveSpec('STRIP', 'Remove leading/trailing whitespace',
                        required_params=['columns']))
_register(PrimitiveSpec('LOWER', 'Convert to lowercase',
                        required_params=['columns']))
_register(PrimitiveSpec('UPPER', 'Convert to uppercase',
                        required_params=['columns']))
_register(PrimitiveSpec('TITLE', 'Convert to title case',
                        required_params=['columns']))
_register(PrimitiveSpec('ROUND', 'Round numeric values',
                        required_params=['columns'],
                        optional_params=['decimals']))
_register(PrimitiveSpec('CLIP', 'Clip values to a range',
                        required_params=['column'],
                        optional_params=['min', 'max']))
_register(PrimitiveSpec('UPDATE_EXTREMUM', 'Update only the maximal or minimal observation(s)',
                        required_params=['column', 'extremum', 'new_value']))
_register(PrimitiveSpec('EXTRACT_DATE_PART', 'Extract part of a date column',
                        required_params=['column', 'part', 'output_column'],
                        modifies_shape=True))
_register(PrimitiveSpec('DATE_DIFF', 'Compute difference between two date columns',
                        required_params=['column_a', 'column_b', 'output_column'],
                        optional_params=['unit'], modifies_shape=True))
_register(PrimitiveSpec('FILL_PATTERN', 'Fill column with an alternating pattern or sequence of values',
                        required_params=['column', 'pattern'],
                        optional_params=['mode'], modifies_shape=False))
_register(PrimitiveSpec('FILL_SEQUENTIAL', 'Fill a column with a sequential integer or numeric series (row numbers, ranges, arithmetic progressions)',
                        required_params=['column'],
                        optional_params=['start', 'step'],
                        modifies_shape=False))
_register(PrimitiveSpec('NORMALIZE_TEXT', 'Trim whitespace and normalize string case in text columns',
                        required_params=['columns'],
                        optional_params=['case'],
                        modifies_shape=False))
_register(PrimitiveSpec('UPDATE_CELL', 'Update a specific cell or rows matching a condition in a column',
                        required_params=['column', 'value'],
                        optional_params=['row_index', 'where_column', 'where_value'],
                        modifies_shape=False))
_register(PrimitiveSpec('SPLIT_COLUMN', 'Split a text column into multiple new columns using a delimiter',
                        required_params=['column', 'delimiter', 'output_columns'],
                        modifies_shape=True))
_register(PrimitiveSpec('CONCAT_COLUMNS', 'Concatenate multiple columns into a new or existing text column with a delimiter',
                        required_params=['columns', 'output_column'],
                        optional_params=['delimiter'],
                        modifies_shape=True))
_register(PrimitiveSpec('MAP_VALUES', 'Map values in a column using a dictionary of old->new mappings',
                        required_params=['column', 'mapping'],
                        optional_params=['default'],
                        modifies_shape=False))
_register(PrimitiveSpec('CUSTOM_TRANSFORM', 'Execute a safe Python/pandas transformation for any complex, custom, or open-ended data transformation',
                        required_params=['code'],
                        optional_params=['columns'],
                        modifies_shape=False))

# ── Deferred capabilities (known but not yet executable) ─────────────────────
DEFERRED_CAPABILITIES = frozenset({
    'GROUP_BY', 'AGGREGATE', 'MERGE', 'CONCAT', 'PIVOT', 'MELT', 'EXPLODE',
})


# ──────────────────────────────────────────────────────────────────────────────
# Simulation result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AffectedCellIR:
    """A cell changed during IR simulation."""
    row_index: int
    column: str
    before: Any
    after: Any


@dataclass
class SimulationResult:
    """Result of compiling and simulating an IR against a DataFrame."""
    success: bool
    result_df: Any = None                     # The transformed DataFrame (or None on failure)
    affected_cells: List[AffectedCellIR] = field(default_factory=list)
    affected_rows: int = 0
    affected_columns: List[str] = field(default_factory=list)
    created_columns: List[str] = field(default_factory=list)
    deleted_columns: List[str] = field(default_factory=list)
    rows_before: int = 0
    rows_after: int = 0
    cols_before: int = 0
    cols_after: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    step_labels: List[str] = field(default_factory=list)
    total_cells_changed: int = 0  # Exact count (affected_cells list is capped at 50)


@dataclass
class ValidationResult:
    """Result of validating a TransformationIR."""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    unsupported: Optional[str] = None  # Set if a deferred capability is needed

"""
IR Planner — LLM-powered transformation plan generation.

Given a natural language request and dataset schema, generates a
TransformationIR by calling the LLM.

The LLM produces structured JSON (Transformation IR), NOT Python code.
The IR is then validated and compiled deterministically.

SAFETY: The LLM never executes code. It only produces a plan.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from transformation.ir import (
    TransformationIR,
    TransformStep,
    PRIMITIVE_REGISTRY,
    DEFERRED_CAPABILITIES,
    ALLOWED_EXPR_TYPES,
    ALLOWED_COMPARE_OPS,
    ALLOWED_ARITHMETIC_OPS,
    ALLOWED_STRING_FNS,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────────────────────────

def _build_ir_planner_prompt(
    user_request: str,
    schema: List[Dict[str, Any]],
    dataset_stats: Optional[Dict] = None,
) -> str:
    """Build the system + user prompt for the LLM IR planner."""

    # Build primitive list
    prim_lines = []
    for name, spec in sorted(PRIMITIVE_REGISTRY.items()):
        prim_lines.append(spec.to_prompt_spec())
    primitives_text = '\n'.join(prim_lines)

    # Build schema summary
    schema_lines = []
    for col in schema:
        dtype = col.get('dtype', 'unknown')
        nullable = col.get('nullable', False)
        schema_lines.append(f"  - {col['name']} ({dtype}){' [nullable]' if nullable else ''}")
    schema_text = '\n'.join(schema_lines) if schema_lines else '  (no columns)'

    prompt = f"""You are the Plexis Transformation Planner. Your job is to convert a user's natural language data transformation request into a structured Transformation IR (Intermediate Representation).

You produce JSON. You do NOT produce Python code.

## AVAILABLE PRIMITIVES

{primitives_text}

## EXPRESSION AST

Expressions are JSON objects with a "type" field. Allowed types:
- COLUMN: {{"type": "COLUMN", "name": "column_name"}}
- LITERAL: {{"type": "LITERAL", "value": 42}}
- COMPARE: {{"type": "COMPARE", "operator": "op", "left": expr, "right": expr}}
  Operators: {sorted(ALLOWED_COMPARE_OPS)}
- ARITHMETIC: {{"type": "ARITHMETIC", "operator": "op", "left": expr, "right": expr}}
  Operators: {sorted(ALLOWED_ARITHMETIC_OPS)}
  For abs: {{"type": "ARITHMETIC", "operator": "abs", "operand": expr}}
- STRING_FN: {{"type": "STRING_FN", "function": "fn", "operand": expr}}
  Functions: {sorted(ALLOWED_STRING_FNS)}
  For replace: add "old" and "new" fields
- CASE: {{"type": "CASE", "conditions": [{{"when": expr, "value": literal_or_expr}}, ...], "default": value}}
- COALESCE: {{"type": "COALESCE", "expressions": [expr1, expr2, ...]}}
- NOT: {{"type": "NOT", "operand": expr}}
- AND: {{"type": "AND", "left": expr, "right": expr}}
- OR: {{"type": "OR", "left": expr, "right": expr}}

## TYPED VALUE LITERALS

Preserve the user's value type in every LITERAL node used to compare or
transform data. Include a `typed_literal` object with `__typed_literal__`
true, `value`, `value_type`, `source_form`, `explicit`, and `was_quoted`.
Use value_type `integer`, `float`, `string`, `boolean`, or `null`. Quoted
values are strings unless the user explicitly supplies a type word; booleans
are not integers; integer and float values compare numerically; strings never
coerce to numbers. Do not discard quotes or type words while building the IR.

## DATASET SCHEMA

{schema_text}

## OUTPUT FORMAT

Return ONLY a JSON object:

{{
  "goal": "Human-readable description of what this transformation does",
  "steps": [
    {{
      "id": "step_1",
      "operation": "PRIMITIVE_NAME",
      "params": {{...}},
      "expression": {{...}},  // only if needed
      "depends_on": [],
      "label": "Human-readable step description"
    }}
  ]
}}

## RULES

1. Only use primitives listed above.
2. Only reference columns that exist in the schema or were created by a previous step.
3. Do NOT invent columns that don't exist.
4. If a column name is ambiguous, pick the most likely match.
5. Use depends_on when a step uses a column created by a previous step.
6. For simple operations (fill, rename, drop, sort, etc.), use the appropriate primitive directly.
7. For computed columns that do NOT exist in the schema, use CREATE_COLUMN with an expression.
8. For modifying existing column values that ALREADY exist in the schema, use MODIFY_COLUMN, FILL, FILL_PATTERN, or REPLACE. NEVER use CREATE_COLUMN if the column already exists in the schema.
9. For cleaning strings (strip + lowercase + etc.), use multiple steps.
10. If the request requires GROUP_BY, AGGREGATE, MERGE, PIVOT, MELT, or EXPLODE, respond with: {{"goal": "...", "steps": [], "metadata": {{"unsupported": "GROUP_BY"}}}}
11. Ensure step IDs are unique.
12. When creating or adding a new column with a constant or default value (e.g. 0, null, or empty string), use CREATE_COLUMN with a LITERAL expression: {{"type": "LITERAL", "value": 0}}.
13. When filling or populating a column with alternating, cyclical, or consecutive values (e.g. 'fill values A and B consecutively', 'alternate yes and no'), use FILL_PATTERN with params: {{"column": ..., "pattern": [...], "mode": "alternating"}}.
14. When filling an EXISTING column with sequential integers or a numeric series (e.g. 'fill sequentially from 1 to end', 'number the rows', 'assign row numbers'), use FILL_SEQUENTIAL with params: {{"column": ..., "start": <int>, "step": <int>}}. NEVER use ADD_COLUMN if the column already exists in the schema.
15. When cleaning, trimming, or normalising text in columns (e.g. 'lowercase the names', 'trim whitespace', 'clean the emails'), use NORMALIZE_TEXT with params: {{"columns": [...], "case": "lower"|"upper"|"title"}}.

## EXAMPLES

### Example 0: Add column with initial/default value
User: "Add a column departure_percent set to 0"
Schema: Departure_Delay (float64), Airline (string)

```json
{{
  "goal": "Add departure_percent column initialized with 0",
  "steps": [
    {{
      "id": "step_1",
      "operation": "CREATE_COLUMN",
      "params": {{"column": "departure_percent"}},
      "expression": {{"type": "LITERAL", "value": 0}},
      "depends_on": [],
      "label": "Create departure_percent column initialized with 0"
    }}
  ]
}}
```

### Example 1: Create age groups
User: "Create an age_group column where under 18 is Minor, 18-64 is Adult, 65+ is Senior"
Schema: age (int64), name (string)

```json
{{
  "goal": "Create age_group column with Minor/Adult/Senior categories",
  "steps": [
    {{
      "id": "step_1",
      "operation": "CREATE_COLUMN",
      "params": {{"column": "age_group"}},
      "expression": {{
        "type": "CASE",
        "conditions": [
          {{
            "when": {{"type": "COMPARE", "operator": "<", "left": {{"type": "COLUMN", "name": "age"}}, "right": {{"type": "LITERAL", "value": 18}}}},
            "value": "Minor"
          }},
          {{
            "when": {{"type": "COMPARE", "operator": "<", "left": {{"type": "COLUMN", "name": "age"}}, "right": {{"type": "LITERAL", "value": 65}}}},
            "value": "Adult"
          }}
        ],
        "default": "Senior"
      }},
      "depends_on": [],
      "label": "Create age_group: <18=Minor, 18-64=Adult, 65+=Senior"
    }}
  ]
}}
```

### Example 2: Clean names
User: "Clean the names by removing spaces, capitalizing, and replacing missing with Unknown"
Schema: name (string) [nullable], age (int64)

```json
{{
  "goal": "Clean name column: strip whitespace, title case, fill missing with Unknown",
  "steps": [
    {{
      "id": "step_1",
      "operation": "FILL",
      "params": {{"columns": ["name"], "value": "Unknown"}},
      "depends_on": [],
      "label": "Fill missing names with 'Unknown'"
    }},
    {{
      "id": "step_2",
      "operation": "STRIP",
      "params": {{"columns": ["name"]}},
      "depends_on": ["step_1"],
      "label": "Remove leading/trailing whitespace from name"
    }},
    {{
      "id": "step_3",
      "operation": "TITLE",
      "params": {{"columns": ["name"]}},
      "depends_on": ["step_2"],
      "label": "Title-case name column"
    }}
  ]
}}
```

### Example 3: Arithmetic column
User: "Create total_price by multiplying quantity and price"
Schema: quantity (int64), price (float64), product (string)

```json
{{
  "goal": "Create total_price = quantity * price",
  "steps": [
    {{
      "id": "step_1",
      "operation": "CREATE_COLUMN",
      "params": {{"column": "total_price"}},
      "expression": {{
        "type": "ARITHMETIC",
        "operator": "multiply",
        "left": {{"type": "COLUMN", "name": "quantity"}},
        "right": {{"type": "COLUMN", "name": "price"}}
      }},
      "depends_on": [],
      "label": "Create total_price = quantity * price"
    }}
  ]
}}
```

### Example 4: Multi-step with dependencies
User: "Create profit from revenue minus cost, then sort by profit descending"
Schema: revenue (float64), cost (float64), product (string)

```json
{{
  "goal": "Create profit column and sort by it descending",
  "steps": [
    {{
      "id": "step_1",
      "operation": "CREATE_COLUMN",
      "params": {{"column": "profit"}},
      "expression": {{
        "type": "ARITHMETIC",
        "operator": "subtract",
        "left": {{"type": "COLUMN", "name": "revenue"}},
        "right": {{"type": "COLUMN", "name": "cost"}}
      }},
      "depends_on": [],
      "label": "Create profit = revenue - cost"
    }},
    {{
      "id": "step_2",
      "operation": "SORT_ROWS",
      "params": {{"column": "profit", "ascending": false}},
      "depends_on": ["step_1"],
      "label": "Sort by profit descending"
    }}
  ]
}}
```

### Example 5: Conditional boolean column
User: "Create is_adult column where age >= 18"
Schema: age (int64), name (string)

```json
{{
  "goal": "Create boolean is_adult column",
  "steps": [
    {{
      "id": "step_1",
      "operation": "CREATE_COLUMN",
      "params": {{"column": "is_adult"}},
      "expression": {{
        "type": "COMPARE",
        "operator": ">=",
        "left": {{"type": "COLUMN", "name": "age"}},
        "right": {{"type": "LITERAL", "value": 18}}
      }},
      "depends_on": [],
      "label": "Create is_adult = (age >= 18)"
    }}
  ]
}}
```

### Example 6: Replace multiple values
User: "Replace USA, US, and United States with United States in country"
Schema: country (string), city (string)

```json
{{
  "goal": "Standardize country values to 'United States'",
  "steps": [
    {{
      "id": "step_1",
      "operation": "REPLACE",
      "params": {{"column": "country", "old_value": "USA", "new_value": "United States"}},
      "depends_on": [],
      "label": "Replace 'USA' with 'United States'"
    }},
    {{
      "id": "step_2",
      "operation": "REPLACE",
      "params": {{"column": "country", "old_value": "US", "new_value": "United States"}},
      "depends_on": ["step_1"],
      "label": "Replace 'US' with 'United States'"
    }}
  ]
}}
```

### Example 7: Extract date part
User: "Extract the year from order_date into a new column"
Schema: order_date (datetime64), product (string), amount (float64)

```json
{{
  "goal": "Extract year from order_date",
  "steps": [
    {{
      "id": "step_1",
      "operation": "EXTRACT_DATE_PART",
      "params": {{"column": "order_date", "part": "year", "output_column": "order_year"}},
      "depends_on": [],
      "label": "Extract year from order_date into order_year"
    }}
  ]
}}
```

### Example 8: Coalesce / null handling
User: "Use the backup_email when email is missing"
Schema: email (string) [nullable], backup_email (string), name (string)

```json
{{
  "goal": "Fill missing emails with backup_email",
  "steps": [
    {{
      "id": "step_1",
      "operation": "MODIFY_COLUMN",
      "params": {{"column": "email"}},
      "expression": {{
        "type": "COALESCE",
        "expressions": [
          {{"type": "COLUMN", "name": "email"}},
          {{"type": "COLUMN", "name": "backup_email"}}
        ]
      }},
      "depends_on": [],
      "label": "Fill missing email with backup_email"
    }}
  ]
}}
```

### Example 9: Round numeric values
User: "Round all prices to 2 decimal places"
Schema: price (float64), quantity (int64)

```json
{{
  "goal": "Round price to 2 decimal places",
  "steps": [
    {{
      "id": "step_1",
      "operation": "ROUND",
      "params": {{"columns": ["price"], "decimals": 2}},
      "depends_on": [],
      "label": "Round price to 2 decimals"
    }}
  ]
}}
```

### Example 10: Unsupported capability
User: "Group sales by region and calculate total revenue"
Schema: region (string), revenue (float64)

```json
{{
  "goal": "Group sales by region with total revenue",
  "steps": [],
  "metadata": {{"unsupported": "GROUP_BY"}}
}}
```

### Example 11: Fill alternating pattern or sequence
User: "in gender columns fill the values male and female consecutively"
Schema: gender (string) [nullable], age (int64)

```json
{{
  "goal": "Fill alternating pattern of 'male' and 'female' in gender column",
  "steps": [
    {{
      "id": "step_1",
      "operation": "FILL_PATTERN",
      "params": {{"column": "gender", "pattern": ["male", "female"], "mode": "alternating"}},
      "depends_on": [],
      "label": "Fill alternating values ['male', 'female'] in 'gender'"
    }}
  ]
}}
```

### Example 12: Remove rows with missing values
User: "delete the rows where values are missing"
Schema: name (string) [nullable], age (int64) [nullable], score (float64) [nullable]

```json
{{
  "goal": "Remove rows that contain any missing values",
  "steps": [
    {{
      "id": "step_1",
      "operation": "FILTER_ROWS",
      "params": {{"action": "KEEP"}},
      "expression": {{
        "type": "AND",
        "left": {{
          "type": "NOT",
          "operand": {{"type": "COMPARE", "operator": "is_null", "left": {{"type": "COLUMN", "name": "name"}}, "right": {{"type": "LITERAL", "value": null}}}}
        }},
        "right": {{
          "type": "NOT",
          "operand": {{"type": "COMPARE", "operator": "is_null", "left": {{"type": "COLUMN", "name": "age"}}, "right": {{"type": "LITERAL", "value": null}}}}
        }}
      }},
      "depends_on": [],
      "label": "Keep only rows where no values are missing"
    }}
  ]
}}
```

### Example 13: Fill existing column sequentially
User: "in column birth year fill the values sequentially from 1 to end"
Schema: id (int64), name (string), birth year (int64)

```json
{{
  "goal": "Fill 'birth year' column with sequential integers starting at 1",
  "steps": [
    {{
      "id": "step_1",
      "operation": "FILL_SEQUENTIAL",
      "params": {{"column": "birth year", "start": 1, "step": 1}},
      "depends_on": [],
      "label": "Fill 'birth year' with sequential numbers 1, 2, 3…"
    }}
  ]
}}
```

### Example 14: Create new column then fill sequentially
User: "add a rank column and number the rows from 1"
Schema: name (string), score (float64)

```json
{{
  "goal": "Create 'rank' column and populate with sequential row numbers",
  "steps": [
    {{
      "id": "step_1",
      "operation": "ADD_COLUMN",
      "params": {{"column": "rank", "value": null}},
      "depends_on": [],
      "label": "Add empty 'rank' column"
    }},
    {{
      "id": "step_2",
      "operation": "FILL_SEQUENTIAL",
      "params": {{"column": "rank", "start": 1, "step": 1}},
      "depends_on": ["step_1"],
      "label": "Fill 'rank' with sequential numbers 1, 2, 3…"
    }}
  ]
}}
```

### Example 15: Normalize / clean text in columns
User: "clean the name and email columns, trim spaces and lowercase them"
Schema: name (string), email (string), age (int64)

```json
{{
  "goal": "Normalize text in 'name' and 'email' columns — trim whitespace and lowercase",
  "steps": [
    {{
      "id": "step_1",
      "operation": "NORMALIZE_TEXT",
      "params": {{"columns": ["name", "email"], "case": "lower"}},
      "depends_on": [],
      "label": "Trim and lowercase 'name' and 'email'"
    }}
  ]
}}
```

## USER REQUEST

{user_request}

Return ONLY the JSON object. No markdown, no explanation, no code fences."""

    return prompt


def _annotate_typed_literals(ir_dict: dict, user_request: str) -> dict:
    """Ensure LITERAL nodes have canonical, JSON-safe semantic metadata."""
    from datasets.semantics.typed_literal import TypedLiteral

    def metadata_for(literal):
        if literal is None:
            value_type = 'null'
        elif isinstance(literal, bool):
            value_type = 'boolean'
        elif isinstance(literal, int):
            value_type = 'integer'
        elif isinstance(literal, float):
            value_type = 'float'
        elif isinstance(literal, str):
            value_type = 'string'
        else:
            literal = str(literal)
            value_type = 'string'
        quoted = isinstance(literal, str) and bool(re.search(
            r"(['\"])" + re.escape(literal) + r"\1", user_request
        ))
        return TypedLiteral(
            value=literal,
            value_type=value_type,
            source_form=repr(literal),
            explicit=quoted,
            was_quoted=quoted,
        ).to_dict()

    def visit(value):
        if isinstance(value, dict):
            if value.get('type') == 'LITERAL' and 'value' in value and not value.get('typed_literal'):
                value['typed_literal'] = metadata_for(value['value'])
            if str(value.get('operation', '')).upper() == 'REPLACE' and isinstance(value.get('params'), dict):
                params = value['params']
                if 'old_value' in params and not params.get('_old_typed'):
                    params['_old_typed'] = metadata_for(params['old_value'])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(ir_dict)
    return ir_dict


# ──────────────────────────────────────────────────────────────────────────────
# IR Planner
# ──────────────────────────────────────────────────────────────────────────────

class IRPlanner:
    """
    LLM-powered transformation planner.

    Converts natural language to TransformationIR by calling the LLM
    with dataset schema and primitive specifications.
    """

    def plan(
        self,
        user_request: str,
        df_columns: List[str],
        df_dtypes: Dict[str, str],
        df_nullable: Optional[Dict[str, bool]] = None,
    ) -> TransformationIR:
        """
        Generate a TransformationIR from a natural language request.

        Args:
            user_request: The user's transformation request
            df_columns: Column names in the DataFrame
            df_dtypes: Column name → dtype string mapping
            df_nullable: Column name → has nulls mapping

        Returns:
            TransformationIR

        Raises:
            ValueError: If the LLM response is unparseable
            RuntimeError: If the LLM call fails
        """
        # Build schema for prompt
        schema = []
        for col in df_columns:
            entry = {
                'name': col,
                'dtype': df_dtypes.get(col, 'unknown'),
            }
            if df_nullable and df_nullable.get(col, False):
                entry['nullable'] = True
            schema.append(entry)

        prompt = _build_ir_planner_prompt(user_request, schema)

        # Call LLM
        from providers.engine import provider_engine
        from providers.domain.contracts import AIRequest

        try:
            response = provider_engine.generate(
                AIRequest(
                    task='transformation_planning',
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=(
                        "You are a transformation planner. You output ONLY valid JSON. "
                        "No markdown. No code fences. No explanation."
                    ),
                    max_tokens=2048,
                )
            )
            if not response or not response.success or not (response.text or "").strip():
                err_msg = response.error if response and response.error else "Model returned an empty response."
                logger.error("[IRPlanner] LLM generation failed: %s", err_msg)
                raise RuntimeError(f"Transformation planning failed: {err_msg}")
            raw_text = response.text.strip()
        except Exception as e:
            logger.error("[IRPlanner] LLM call failed: %s", e)
            raise RuntimeError(f"Transformation planning failed: {e}") from e

        # Parse the LLM response
        ir_dict = self._parse_response(raw_text)
        ir_dict = _annotate_typed_literals(ir_dict, user_request)

        # Check for unsupported capability
        unsupported = ir_dict.get('metadata', {}).get('unsupported')
        if unsupported:
            return TransformationIR(
                goal=ir_dict.get('goal', user_request),
                steps=[],
                metadata={'unsupported': unsupported},
            )

        # Build IR
        ir = TransformationIR.from_dict(ir_dict)
        if not ir.goal:
            ir.goal = user_request

        logger.info(
            "[IRPlanner] Generated IR: goal='%s' steps=%d",
            ir.goal, len(ir.steps),
        )
        return ir

    def _parse_response(self, raw_text: str) -> dict:
        """Parse LLM response into a dict, with recovery for common issues."""
        if not raw_text or not raw_text.strip():
            raise ValueError("Model returned an empty response. Please try again.")

        text = raw_text.strip()

        # 1. Try extracting content inside markdown code fences ```json ... ``` or ``` ... ```
        fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if fence_match:
            fence_content = fence_match.group(1).strip()
            try:
                parsed = json.loads(fence_content)
                if isinstance(parsed, dict):
                    return self._validate_ir_dict(parsed)
            except json.JSONDecodeError:
                pass

        # 2. Try direct parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return self._validate_ir_dict(parsed)
        except json.JSONDecodeError:
            pass

        # 3. Try to extract JSON object between the outermost curly braces
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace:last_brace + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return self._validate_ir_dict(parsed)
            except json.JSONDecodeError:
                # 4. Try truncated JSON recovery on candidate: close unclosed brackets
                for suffix in ['}]}', ']}', '}]}}', '}}']:
                    try:
                        parsed = json.loads(candidate + suffix)
                        if isinstance(parsed, dict):
                            return self._validate_ir_dict(parsed)
                    except json.JSONDecodeError:
                        continue

        # 5. Try truncated JSON recovery on text
        for suffix in ['}]}', ']}', '}]}}', '}}']:
            try:
                parsed = json.loads(text + suffix)
                if isinstance(parsed, dict):
                    return self._validate_ir_dict(parsed)
            except json.JSONDecodeError:
                continue

        preview = text[:300].strip()
        raise ValueError(
            f"Could not parse LLM response as JSON. Response preview:\n{preview}"
        )

    def _validate_ir_dict(self, d: dict) -> dict:
        """Validate and normalize basic IR dictionary schema."""
        if not isinstance(d, dict):
            raise ValueError("Parsed JSON is not an object.")
        # Normalize alternative keys for steps if steps missing
        if 'steps' not in d:
            if 'transformations' in d and isinstance(d['transformations'], list):
                d['steps'] = d.pop('transformations')
            elif 'operations' in d and isinstance(d['operations'], list):
                d['steps'] = d.pop('operations')
            elif 'plan' in d and isinstance(d['plan'], list):
                d['steps'] = d.pop('plan')
            else:
                d['steps'] = []
        if not isinstance(d.get('steps'), list):
            raise ValueError("Transformation plan 'steps' must be a list.")
        if 'goal' not in d or not d['goal']:
            d['goal'] = "Applied transformation"
        return d


ir_planner = IRPlanner()

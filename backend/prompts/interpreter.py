"""
Interpreter Prompt â€” the 80% LLM semantic decision layer.

This prompt is the PRIMARY semantic authority for intent classification.
It receives the full structured context from SemanticContextBuilder and
must return a typed JSON interpretation.

ARCHITECTURE CONTRACT:
  - The LLM makes the FINAL semantic decision.
  - Keywords and schema signals are supporting evidence, not the decision.
  - The output must be valid JSON with the specified schema.
  - The output is schema-validated before use.

LLM OUTPUT SCHEMA:
  {
    "intent": "DATA_OPERATION" | "TRANSFORM" | "INVESTIGATE" | "CONVERSATION" | "EXPLANATION" | "TITLE",
    "operations": [
      {
        "type":   string (e.g. "MIN", "MAX", "TOP_N", "FILTER", "FIND_DUPLICATES",
                          "FILL_MISSING", "DROP_DUPLICATES", "RENAME_COLUMN", "DROP_COLUMN",
                          "CAST_DTYPE", "REPLACE_VALUES", "SORT_ROWS", "GET_COLUMN_NAMES", "GET_DATA_TYPES", "GET_SCHEMA", 
                          "MISSING_VALUES", "UNIQUE_VALUES", "DATASET_PROFILE", "NON_NULL_COUNTS", "MISSING_PERCENTAGES"),
      "column": string (column label, required for single-column operations),
      "columns": [string] (for multi-column ops like FIND_DUPLICATES),
      "n":      integer (for TOP_N/BOTTOM_N),
      "order":  "asc" | "desc" (for TOP_N/BOTTOM_N and SORT_ROWS),
      "func":   string (for AGGREGATE: "mean"/"median"/"std"/"sum"/"count"),
      "operator": string (for FILTER: ">","<",">=","<=","==","!="),
      "value":  any (for FILTER/REPLACE_VALUES),
      "new_name": string (for RENAME_COLUMN),
      "dtype":  string (for CAST_DTYPE: "int"/"float"/"str"/"bool"),
      "fill_value": any (for FILL_MISSING)
    } | null,
    "confidence": float (0.0â€“1.0),
    ],
    "confidence": float (0.0-1.0),
    "reason":   string (short explanation of interpretation)
  }

INTENT VALUES:
  DATA_OPERATION â€” user wants a deterministic READ calculation on the dataset (no mutation)
  TRANSFORM      â€” user wants to MUTATE the dataset (fill nulls, rename columns, drop rows, cast types, etc.)
  INVESTIGATE    â€” user wants a full autonomous investigation/analysis report of the dataset
  CONVERSATION   â€” general knowledge, casual chat, conceptual question
  EXPLANATION    â€” explain a previous result (workspace_action present)
  TITLE          â€” generate a conversation title

NOTE: TRANSFORM operations are DATA-MUTATING. DATA_OPERATION operations are READ-ONLY.
NOTE: The operations array is populated for DATA_OPERATION and TRANSFORM intents. If there are multiple distinct questions/actions, include multiple objects in the operations array.
"""
from __future__ import annotations
from typing import Dict, Any, Optional, List

import json


def build_interpreter_prompt(
    query: str,
    semantic_context: dict,
) -> str:
    """
    Build the LLM interpretation prompt.

    Args:
        query: The normalized user query.
        semantic_context: Dict from SemanticContextBuilder.build().

    Returns:
        A prompt string for the LLM classification/interpretation call.
    """
    dataset_loaded = semantic_context.get("dataset_loaded", False)
    dataset_filename = semantic_context.get("dataset_filename") or "unnamed dataset"
    columns = semantic_context.get("columns", [])
    referenced_cols = semantic_context.get("referenced_columns", [])
    compatible_caps = semantic_context.get("compatible_capabilities", [])
    workspace_ctx = semantic_context.get("workspace_context", {})

    # â”€â”€ Build column schema block â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if dataset_loaded and columns:
        col_lines = []
        for col in columns[:30]:  # Limit to 30 columns in prompt
            label = col.get("name") or col.get("label", "")
            ctype = col.get("type", "unknown")
            samples = col.get("samples", [])
            sample_str = f" (e.g. {', '.join(str(s) for s in samples[:2])})" if samples else ""
            col_lines.append(f"  - {label} [{ctype}]{sample_str}")
        col_block = "DATASET SCHEMA:\n" + "\n".join(col_lines)
        ref_block = (
            f"\nCOLUMNS REFERENCED IN QUERY: {', '.join(referenced_cols)}"
            if referenced_cols else ""
        )
    else:
        col_block = "NO DATASET LOADED."
        ref_block = ""

    # â”€â”€ Build capabilities block â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if dataset_loaded and compatible_caps:
        cap_lines = [
            f"  - {c['type']}: {c['description']}"
            for c in compatible_caps[:15]  # Cap at 15
        ]
        cap_block = "AVAILABLE ANALYTICAL CAPABILITIES:\n" + "\n".join(cap_lines)
    else:
        cap_block = ""

    # â”€â”€ Build workspace context block â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    ws_lines = []
    active_action = workspace_ctx.get("active_action")
    if active_action:
        ws_lines.append(f"  Active action: {active_action.get('type')} â€” {active_action.get('description', '')[:80]}")
    recent_ops = workspace_ctx.get("recent_operations", [])
    if recent_ops:
        ops_str = ", ".join(
            f"{op.get('type')}({op.get('column', '')})" for op in recent_ops
        )
        ws_lines.append(f"  Recent operations: {ops_str}")
    selected_rows = workspace_ctx.get("selected_rows", [])
    if selected_rows:
        ws_lines.append(f"  Selected rows: {selected_rows[:10]} ({workspace_ctx.get('selected_row_count', 0)} total)")
    ws_block = ("WORKSPACE CONTEXT:\n" + "\n".join(ws_lines)) if ws_lines else ""

    # â”€â”€ Build the full prompt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    sections = [
        s for s in [col_block, ref_block, cap_block, ws_block] if s
    ]
    context_block = "\n\n".join(sections) if sections else "(no additional context)"

    # â”€â”€ Interpretation rules â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    dataset_rule = (
        "\nROUTING RULES FOR DATASET QUESTIONS:\n"
        "  READ operations (DATA_OPERATION):\n"
        "    min/max/average/count/top/bottom/missing/duplicates/filter/sort/\n"
        "    correlation/outliers/standard deviation on any listed column.\n"
        "  MUTATING operations (TRANSFORM) â€” dataset IS loaded:\n"
        "    fill missing values, fill nulls, impute, drop duplicates, rename column,\n"
        "    drop column, remove column, delete column, cast type, convert type,\n"
        "    replace values, filter rows (to keep/delete), sort the dataset.\n"
        "    â†’ TRANSFORM when the user explicitly wants to CHANGE the data.\n"
        "  MULTI-OPERATION EXAMPLES:\n"
        "  Q: \"What is the average age and how many missing values are there in total?\"\n"
        "  -> {\"intent\": \"DATA_OPERATION\", \"operations\": [{\"type\": \"AGGREGATE\", \"column\": \"age\", \"func\": \"mean\"}, {\"type\": \"MISSING_VALUES\", \"columns\": []}], \"confidence\": 0.98, \"reason\": \"Two distinct read-only queries\"}\n"
        "  Q: \"Drop the id column and rename sales to revenue\"\n"
        "  -> {\"intent\": \"TRANSFORM\", \"operations\": [{\"type\": \"DROP_COLUMN\", \"column\": \"id\"}, {\"type\": \"RENAME_COLUMN\", \"column\": \"sales\", \"new_name\": \"revenue\"}], \"confidence\": 0.98, \"reason\": \"Two distinct mutations\"}\n"
        "  INVESTIGATE:\n"
        "    'analyze the dataset', 'run an investigation', 'investigate this data',\n"
        "    'give me a full analysis', 'what insights does this data have' â†’ INVESTIGATE.\n"
        "  If the user asks for a column that does NOT exist in the DATASET SCHEMA (e.g. salary), YOU MUST return CONVERSATION intent. Do NOT substitute it.\n"
    ) if dataset_loaded else (
        "\nROUTING RULES (NO DATASET):\n"
        "  - No dataset is loaded. Do not invent data results.\n"
        "  - If the user asks about data that would require a dataset, use CONVERSATION\n"
        "    and suggest they upload a dataset.\n"
    )

    operation_schema = """
OPERATION SCHEMA (use when intent=DATA_OPERATION or intent=TRANSFORM):

  READ-ONLY operations (DATA_OPERATION â€” does NOT mutate the dataset):
    {"type": "MIN", "column": "column_name"}
    {"type": "MAX", "column": "column_name"}
    {"type": "AGGREGATE", "column": "column_name", "func": "mean"}
    {"type": "TOP_N", "column": "column_name", "n": 10, "order": "desc"}
    {"type": "FILTER", "column": "column_name", "operator": ">", "value": 50}
    {"type": "FIND_DUPLICATES", "columns": ["token"]}
    {"type": "MISSING_VALUES", "columns": ["token", "contact"]}
    {"type": "DATASET_PROFILE", "columns": []}
    {"type": "GET_COLUMN_NAMES", "columns": []}
    {"type": "DESCRIBE_COLUMN", "column": "column_name", "metrics": ["mean", "median", "min", "max"]}
    {"type": "VALUE_COUNTS", "column": "column_name"}
    {"type": "RANGE", "column": "column_name"}
    {"type": "UNIQUE_COUNT", "column": "column_name"}
    {"type": "MOST_FREQUENT_VALUE", "column": "column_name"}
    {"type": "LEAST_FREQUENT_VALUE", "column": "column_name"}
    {"type": "GET_ROW_COUNT"}
    {"type": "GET_COLUMN_COUNT"}
    {"type": "MISSING_COUNT_TOTAL"}
    {"type": "MISSING_COUNT_COLUMN", "column": "column_name"}
    {"type": "COLUMN_WITH_MOST_MISSING_VALUES"}
    {"type": "COLUMNS_WITHOUT_MISSING_VALUES"}
    {"type": "GROUP_MEAN", "column": "group_col", "value_column": "value_col", "top_n": 10, "order": "desc"}
    {"type": "GROUP_SUM", "column": "group_col", "value_column": "value_col", "top_n": 10, "order": "desc"}
    {"type": "GROUP_COUNT", "column": "group_col", "top_n": 10, "order": "desc"}
    {"type": "CORRELATION", "column": "col1", "columns": ["col2"]}
    {"type": "QUANTILES", "column": "column_name", "quantiles": [0.25, 0.5, 0.75]}
    {"type": "HYPOTHETICAL", "column": "column_name", "action": "fill_missing", "fill_value": 5.0, "metric": "mean"}
    {"type": "HYPOTHETICAL", "column": "column_name", "action": "filter", "operator": ">", "filter_value": 50, "metric": "mean"}
    {"type": "DUPLICATE_COUNT"}
    {"type": "HAS_DUPLICATES"}


  MUTATING operations (TRANSFORM â€” changes the dataset, requires user approval):
    {"type": "FILL_MISSING", "columns": ["score", "price"], "value": 0}
    {"type": "FILL_MISSING", "columns": ["score"], "value": "mean"}
    {"type": "FILL_MISSING", "columns": ["score"], "value": "median"}
    {"type": "FILL_MISSING", "columns": ["score"], "value": "mode"}
    {"type": "FILL_MISSING", "columns": ["score"], "value": "ffill"}
    {"type": "DROP_DUPLICATES", "columns": ["token"]}
    {"type": "RENAME_COLUMN", "column": "old_name", "new_name": "new_name"}
    {"type": "DROP_COLUMN", "column": "column_name"}
    {"type": "CAST_DTYPE", "column": "column_name", "dtype": "int"}
    {"type": "REPLACE_VALUES", "column": "column_name", "old_value": "North", "new_value": "New North"}
    {"type": "REPLACE_VALUES", "old_value": "North", "new_value": "New North"}
    {"type": "SORT_ROWS", "column": "column_name", "ascending": true}
    {"type": "SORT_ROWS", "column": "column_name", "ascending": false}
    {"type": "FILTER_ROWS", "column": "score", "operator": ">", "value": 30, "action": "KEEP"}
    {"type": "FILTER_ROWS", "column": "score", "operator": "<", "value": 18, "action": "REMOVE"}
    {"type": "CLIP", "column": "column_name", "max": 60}
    {"type": "CLIP", "column": "column_name", "min": 10}
    {"type": "UPDATE_EXTREMUM", "column": "column_name", "extremum": "max", "new_value": 60}
    {"type": "UPDATE_EXTREMUM", "column": "column_name", "extremum": "min", "new_value": 10}
    {"type": "EXTREME_VALUE_AMBIGUITY", "column": "column_name", "extremum": "max", "target_value": 60}

  IMPORTANT KEYS:
    - FILL_MISSING uses "value" (not "fill_value"): number, or "mean"/"median"/"mode"/"ffill"/"bfill"
    - REPLACE_VALUES uses "old_value" and "new_value" (not "value")
    - SORT_ROWS uses "ascending": true/false (not "order")
    - FILTER_ROWS uses "action": "KEEP" or "REMOVE"
    - DROP_COLUMN uses "column" (singular string, not "columns")
    - EXPLICIT QUOTES: If the user wraps a value in quotes (like 'none' or "false"), you MUST preserve it as a string exactly as written. DO NOT evaluate it to a JSON null or boolean.
    - NEVER translate comparative words ("highest", "lowest", "maximum", "minimum", "max", "min") into literal string values in REPLACE_VALUES unless the user explicitly requested literal text replacement (e.g. 'replace the word "max" with...').
    - If the user asks "change the highest X to Y" or "change the lowest X to Y", this is ambiguous between capping and modifying only the maximal observation(s). Use EXTREME_VALUE_AMBIGUITY so the user is prompted to clarify.
    - RANKING ENTITY VS METRIC RESOLUTION:
      * When user asks for top/bottom N of an entity/category with an EXPLICIT aggregation:
        - "average", "mean", "avg" -> GROUP_MEAN with {"type": "GROUP_MEAN", "column": "<entity_col>", "value_column": "<metric_col>", "top_n": N, "order": "desc"}
        - "maximum", "max", "highest single" -> GROUP_MAX with {"type": "GROUP_MAX", "column": "<entity_col>", "value_column": "<metric_col>", "top_n": N, "order": "desc"}
        - "minimum", "min", "lowest single" -> GROUP_MIN with {"type": "GROUP_MIN", "column": "<entity_col>", "value_column": "<metric_col>", "top_n": N, "order": "asc"}
        - "median" -> GROUP_MEDIAN with {"type": "GROUP_MEDIAN", "column": "<entity_col>", "value_column": "<metric_col>", "top_n": N, "order": "desc"}
        - "total", "sum", "sales", "revenue", "profit" -> GROUP_SUM with {"type": "GROUP_SUM", "column": "<entity_col>", "value_column": "<metric_col>", "top_n": N, "order": "desc"}
        - "count", "frequency", "most frequent" -> GROUP_COUNT with {"type": "GROUP_COUNT", "column": "<entity_col>", "top_n": N, "order": "desc"}
        - "delayed flights", "count of positive delay" -> GROUP_COUNT_FILTERED with {"type": "GROUP_COUNT_FILTERED", "column": "<entity_col>", "filter_column": "<metric_col>", "filter_op": ">", "filter_value": 0, "top_n": N, "order": "desc"}
      * When user asks for top/bottom N of an entity/category by a metric WITHOUT specifying an aggregation (e.g. "top 10 most delayed airlines", "least delayed airlines"):
        DO NOT SILENTLY DEFAULT TO GROUP_MEAN OR GUESS AN AGGREGATION!
        This request is ambiguous between average delay, count of delayed flights, and maximum delay.
      * When user asks for top/bottom N individual rows, records, or metric observations (e.g. "top 10 most delayed flights", "top 10 departure delays", "highest 5 scores"):
        Use TOP_N or BOTTOM_N:
        {"type": "TOP_N", "column": "<metric_col>", "n": N, "order": "desc"}
      * When user asks for a single extreme value (e.g. "highest departure delay", "maximum price", "lowest score"):
        Use MAX or MIN:
        {"type": "MAX", "column": "<metric_col>"}
"""

    examples = """
EXAMPLES:

  READ-ONLY (DATA_OPERATION):
  Q: "give me top 10 airlines by average departure delay"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GROUP_MEAN", "column": "Airline", "value_column": "Departure_Delay", "top_n": 10, "order": "desc"}], "confidence": 0.98, "reason": "Rank airlines by average departure delay descending, top 10"}

  Q: "which are the top 5 cities with highest sales?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GROUP_SUM", "column": "city", "value_column": "sales", "top_n": 5, "order": "desc"}], "confidence": 0.98, "reason": "Rank cities by total sales descending, top 5"}

  Q: "give me top 10 most delayed flights" / "top 10 highest delays"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "TOP_N", "column": "Departure_Delay", "n": 10, "order": "desc"}], "confidence": 0.98, "reason": "Top 10 individual flight delay observations"}

  Q: "what is the highest departure delay?" / "maximum departure delay"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MAX", "column": "Departure_Delay"}], "confidence": 0.98, "reason": "Maximum scalar value of Departure_Delay"}

  Q: "what is the lowest score?"
  â†’ {"intent": "DATA_OPERATION", "operations": [{"type": "MIN", "column": "score"}], "confidence": 0.97, "reason": "Lowest = minimum; score column is numeric"}

  Q: "how many non-null values does each column have?" / "count non-nulls"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "NON_NULL_COUNTS"}], "confidence": 0.95, "reason": "Counting non-nulls"}

  Q: "how many non-null age values are there?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "NON_NULL_COUNTS", "column": "age"}], "confidence": 0.95, "reason": "Count non-null values for a specific column"}

  Q: "what percentage of values are missing in each column?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MISSING_PERCENTAGES"}], "confidence": 0.95, "reason": "Missing percentages"}

  Q: "give me a quick data-quality summary"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "DATASET_PROFILE"}], "confidence": 0.95, "reason": "Dataset profile summary"}

  Q: "which city appears most frequently?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MOST_FREQUENT_VALUE", "column": "city"}], "confidence": 0.95, "reason": "Finding mode"}

  Q: "which city appears least frequently?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "LEAST_FREQUENT_VALUE", "column": "city"}], "confidence": 0.95, "reason": "Finding least common"}

  Q: "what is the range of the age column?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "RANGE", "column": "age"}], "confidence": 0.95, "reason": "Range calculation"}

  Q: "how many unique cities are there?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "UNIQUE_COUNT", "column": "city"}], "confidence": 0.95, "reason": "Counting unique values"}

  Q: "what percentage of the records come from each city?" / "distribution of city" / "most common values in city" / "how many records belong to each city"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "VALUE_COUNTS", "column": "city"}], "confidence": 0.95, "reason": "Distribution of categories"}

  Q: "what is the average score?"
  â†’ {"intent": "DATA_OPERATION", "operations": [{"type": "AGGREGATE", "column": "score", "func": "mean"}], "confidence": 0.97, "reason": "Average = mean aggregate on score"}

  Q: "what are the columns?" / "list all fields" / "what columns does this have?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_COLUMN_NAMES", "columns": []}], "confidence": 0.98, "reason": "User is asking for column names"}

  Q: "how many columns are there?" / "what is the column count?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_COLUMN_COUNT", "columns": []}], "confidence": 0.98, "reason": "User is asking for column count"}

  Q: "what are the data types of each column?" / "show me the data types"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_DATA_TYPES", "columns": []}], "confidence": 0.98, "reason": "User is asking for data types"}

  Q: "show me the complete schema"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_SCHEMA", "columns": []}], "confidence": 0.98, "reason": "User is asking for complete schema"}

  Q: "which columns contain missing values?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "COLUMNS_WITH_MISSING_VALUES"}], "confidence": 0.95, "reason": "Find columns that have NA"}

  Q: "what percentage of the dataset is complete?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "DATASET_COMPLETENESS"}], "confidence": 0.95, "reason": "Dataset completion percentage"}

  Q: "how many unique values does each column have?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "UNIQUE_COUNTS"}], "confidence": 0.95, "reason": "Unique counts across all columns"}

  Q: "which column has the most unique values?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "COLUMN_WITH_MOST_UNIQUE_VALUES"}], "confidence": 0.95, "reason": "Max unique value column"}

  Q: "which column has the fewest unique values?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "COLUMN_WITH_FEWEST_UNIQUE_VALUES"}], "confidence": 0.95, "reason": "Min unique value column"}

  Q: "which columns are numeric?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_NUMERIC_COLUMNS", "columns": []}], "confidence": 0.98, "reason": "User is asking for numeric columns"}

  Q: "which columns are text columns?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_TEXT_COLUMNS", "columns": []}], "confidence": 0.98, "reason": "User is asking for text columns"}

  Q: "how many rows are in the dataset?" / "how many rows are there?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_ROW_COUNT"}], "confidence": 0.98, "reason": "Asking for total dataset length"}

  Q: "how many missing values are there?" / "what are the missing values?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MISSING_VALUES", "columns": []}], "confidence": 0.95, "reason": "Counting missing values by column"}

  Q: "how many missing values are there in total?" / "total missing cells"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MISSING_COUNT_TOTAL"}], "confidence": 0.95, "reason": "Total count of missing values"}

  Q: "how many missing age values are there?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MISSING_COUNT_COLUMN", "column": "age"}], "confidence": 0.95, "reason": "Missing values for a specific column"}

  Q: "how many duplicates are there?" / "what is the duplicate count?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "DUPLICATE_COUNT"}], "confidence": 0.95, "reason": "Counting total duplicates"}

  Q: "are there any duplicate rows?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "HAS_DUPLICATES"}], "confidence": 0.95, "reason": "Checking if any duplicates exist"}

  Q: "find the duplicate rows" / "show me the duplicates"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "FIND_DUPLICATES", "columns": []}], "confidence": 0.95, "reason": "Finding duplicate rows"}

  Q: "what is the average score?"
  â†’ {"intent": "DATA_OPERATION", "operations": [{"type": "AGGREGATE", "column": "score", "func": "mean"}], "confidence": 0.97, "reason": "Average = mean aggregate on score"}

  Q: "what are the columns?" / "list all fields" / "what columns does this have?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_COLUMN_NAMES", "columns": []}], "confidence": 0.98, "reason": "User is asking for column names"}

  Q: "how many columns are there?" / "what is the column count?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_COLUMN_COUNT", "columns": []}], "confidence": 0.98, "reason": "User is asking for column count"}

  Q: "what are the data types of each column?" / "show me the data types"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_DATA_TYPES", "columns": []}], "confidence": 0.98, "reason": "User is asking for data types"}

  Q: "show me the complete schema"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_SCHEMA", "columns": []}], "confidence": 0.98, "reason": "User is asking for complete schema"}

  Q: "which columns are numeric?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_NUMERIC_COLUMNS", "columns": []}], "confidence": 0.98, "reason": "User is asking for numeric columns"}

  Q: "which columns are text columns?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_TEXT_COLUMNS", "columns": []}], "confidence": 0.98, "reason": "User is asking for text columns"}

  Q: "how many rows are in the dataset?" / "how many rows are there?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "GET_ROW_COUNT"}], "confidence": 0.98, "reason": "Asking for total dataset length"}

  Q: "how many missing values are there?" / "what are the missing values?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MISSING_VALUES", "columns": []}], "confidence": 0.95, "reason": "Counting missing values by column"}

  Q: "how many missing values are there in total?" / "total missing cells"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MISSING_COUNT_TOTAL"}], "confidence": 0.95, "reason": "Total count of missing values"}

  Q: "how many missing age values are there?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "MISSING_COUNT_COLUMN", "column": "age"}], "confidence": 0.95, "reason": "Missing values for a specific column"}

  Q: "how many duplicates are there?"
  â†’ {"intent": "DATA_OPERATION", "operations": [{"type": "FIND_DUPLICATES", "columns": []}], "confidence": 0.95, "reason": "Counting duplicates is a read-only inspection"}

  TRANSFORM â€” FILL_MISSING:
  Q: "fill missing values with 0"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "FILL_MISSING", "value": 0}], "confidence": 0.98, "reason": "User wants to mutate dataset by filling nulls with 0"}

  Q: "fill missing score values with the median"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "FILL_MISSING", "columns": ["score"], "value": "median"}], "confidence": 0.97, "reason": "Fill score nulls with statistical imputation (median)"}

  Q: "fill nulls in score and price with mean"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "FILL_MISSING", "columns": ["score", "price"], "value": "mean"}], "confidence": 0.97, "reason": "Fill multiple columns with mean imputation"}

  TRANSFORM â€” DROP_DUPLICATES:
  Q: "remove duplicate tokens"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "DROP_DUPLICATES", "columns": ["token"]}], "confidence": 0.95, "reason": "User wants to mutate dataset by dropping duplicate rows"}

  Q: "get rid of duplicate records"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "DROP_DUPLICATES"}], "confidence": 0.94, "reason": "Remove duplicate rows across all columns"}

  TRANSFORM â€” RENAME_COLUMN:
  Q: "rename the label column to full_label"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "RENAME_COLUMN", "column": "label", "new_name": "full_label"}], "confidence": 0.98, "reason": "Explicit column rename â€” data mutation"}

  Q: "call the score column user_score"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "RENAME_COLUMN", "column": "score", "new_name": "user_score"}], "confidence": 0.96, "reason": "Rename via natural language synonym"}

  TRANSFORM â€” DROP_COLUMN:
  Q: "drop the contact column"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "DROP_COLUMN", "column": "contact"}], "confidence": 0.97, "reason": "User wants to remove a column from the dataset"}

  Q: "remove the contact column"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "DROP_COLUMN", "column": "contact"}], "confidence": 0.97, "reason": "Remove = drop column"}

  TRANSFORM â€” CAST_DTYPE:
  Q: "make all ages integers"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "CAST_DTYPE", "column": "score", "dtype": "int"}], "confidence": 0.96, "reason": "Convert column type to integer"}

  Q: "convert price to float"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "CAST_DTYPE", "column": "price", "dtype": "float"}], "confidence": 0.97, "reason": "Type conversion to float"}

  TRANSFORM â€” REPLACE_VALUES:
  Q: "replace North with New North in region"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "REPLACE_VALUES", "column": "region", "old_value": "North", "new_value": "New North"}], "confidence": 0.97, "reason": "Replace specific values in a column"}

  Q: "change East to West"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "REPLACE_VALUES", "old_value": "East", "new_value": "West"}], "confidence": 0.96, "reason": "Replace values â€” column will be auto-inferred"}

  TRANSFORM â€” SORT_ROWS:
  Q: "sort by price descending"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "SORT_ROWS", "column": "price", "ascending": false}], "confidence": 0.97, "reason": "Sort the dataset by price in descending order"}

  Q: "sort highest price first"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "SORT_ROWS", "column": "price", "ascending": false}], "confidence": 0.96, "reason": "Highest first = descending sort"}

  TRANSFORM â€” FILTER_ROWS:
  Q: "keep rows where score > 30"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "FILTER_ROWS", "column": "score", "operator": ">", "value": 30, "action": "KEEP"}], "confidence": 0.97, "reason": "Keep matching rows, remove the rest"}

  Q: "remove rows younger than 18"
  â†’ {"intent": "TRANSFORM", "operations": [{"type": "FILTER_ROWS", "column": "score", "operator": "<", "value": 18, "action": "REMOVE"}], "confidence": 0.97, "reason": "Remove rows matching condition"}

  Q: "delete rows where region is Central"
  → {"intent": "TRANSFORM", "operations": [{"type": "FILTER_ROWS", "column": "region", "operator": "==", "value": "Central", "action": "REMOVE"}], "confidence": 0.96, "reason": "Delete specific rows"}

  TRANSFORM — EXTREME_VALUE_AMBIGUITY:
  Q: "change the highest departure delay to 60"
  → {"intent": "TRANSFORM", "operations": [{"type": "EXTREME_VALUE_AMBIGUITY", "column": "Departure_Delay", "extremum": "max", "target_value": 60}], "confidence": 0.98, "reason": "Ambiguous between capping values above 60 and updating only the maximum row(s)"}

  Q: "set the lowest price to 10"
  → {"intent": "TRANSFORM", "operations": [{"type": "EXTREME_VALUE_AMBIGUITY", "column": "price", "extremum": "min", "target_value": 10}], "confidence": 0.98, "reason": "Ambiguous between flooring values below 10 and updating only the minimum row(s)"}

  MULTI-OPERATION EXAMPLES:
  Q: "What is the average age and how many missing values are there in total?"
  -> {"intent": "DATA_OPERATION", "operations": [{"type": "AGGREGATE", "column": "age", "func": "mean"}, {"type": "MISSING_VALUES", "columns": []}], "confidence": 0.98, "reason": "Two distinct read-only queries"}
  Q: "Drop the id column and rename sales to revenue"
  -> {"intent": "TRANSFORM", "operations": [{"type": "DROP_COLUMN", "column": "id"}, {"type": "RENAME_COLUMN", "column": "sales", "new_name": "revenue"}], "confidence": 0.98, "reason": "Two distinct mutations"}

  INVESTIGATE:
  Q: "investigate the dataset" / "analyze this dataset" / "run an investigation"
  â†’ {"intent": "INVESTIGATE", "operations": [], "confidence": 0.96, "reason": "User wants a full autonomous investigation report"}

  CONVERSATION:
  Q: "what is standard deviation?" (general knowledge, no specific column)
  â†’ {"intent": "CONVERSATION", "operations": [], "confidence": 0.88, "reason": "Conceptual question about statistics"}

  Q: "what happens if I fill the missing values?"
  â†’ {"intent": "CONVERSATION", "operations": [], "confidence": 0.85, "reason": "Hypothetical question, not an action request"}

  Q: "hello, how are you?"
  â†’ {"intent": "CONVERSATION", "operations": [], "confidence": 0.99, "reason": "Greeting"}

  AMBIGUOUS (pay attention to these):
  Q: "How many missing values are there?" â†’ DATA_OPERATION (read-only inspection)
  Q: "Fill the missing values with 0" â†’ TRANSFORM (mutation)
  Q: "How many duplicates are there?" â†’ DATA_OPERATION (read-only inspection)
  Q: "Remove duplicate rows" â†’ TRANSFORM (mutation)
  Q: "What is the average score?" â†’ DATA_OPERATION (read-only calculation)
  Q: "Fill missing score with the average" â†’ TRANSFORM (mutation using mean)
  Q: "Sort by score" â†’ TRANSFORM (reorders dataset)
  Q: "Show me the top 10 by price" â†’ DATA_OPERATION (read-only display)
"""

    prompt = f"""You are the Plexis semantic intent interpreter.

Your job: interpret the user's query and return a JSON interpretation.

USER QUERY: "{query}"

CONTEXT:
{context_block}
{dataset_rule}
{operation_schema}
{examples}
RETURN ONLY a single valid JSON object â€” no prose, no markdown fences.
Required keys: intent, operations, confidence, reason.
intent must be exactly one of: DATA_OPERATION, TRANSFORM, INVESTIGATE, CONVERSATION, EXPLANATION, TITLE
"""

    return prompt.strip()

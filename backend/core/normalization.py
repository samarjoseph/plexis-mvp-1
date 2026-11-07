import re
import string
from typing import List
from core.context import ExecutionContext

class QueryNormalizer:
    """Normalizes the user query for routing and execution."""
    
    def __init__(self):
        # Basic mapping for normalization
        self.replacements = {
            r'\btopper\b': 'highest',
            r'\bavg\b': 'average',
            r'\bmax\b': 'maximum',
            r'\bmin\b': 'minimum'
        }
        
        # Operation keywords mapping
        self.operation_keywords = {
            'sort': ['sort', 'order', 'arrange', 'rank'],
            'find': ['find', 'search', 'get', 'show', 'list', 'display', 'filter'],
            'calculate': ['calculate', 'compute', 'average', 'sum', 'count', 'mean', 'median', 'total'],
            'plot': ['plot', 'graph', 'chart', 'visualize', 'draw']
        }
        
        # Vague queries that trigger high ambiguity
        self.vague_queries = ['do it', 'help', 'hi', 'hello', 'test', 'what', 'why', 'how']

    def normalize(self, context: ExecutionContext) -> None:
        """
        Normalize the incoming query in the context.
        """
        raw_query = context.message
        
        if not raw_query:
            context.normalized_query = ""
            context.ambiguity_score = 1.0
            return
            
        # 1. Clean input
        # Lowercase
        cleaned = raw_query.lower()
        # Separate hyphenated/dashed/slashed compound terms with spaces
        cleaned = re.sub(r'[-/–—]', ' ', cleaned)
        # Remove sentence punctuation while preserving relational operators and identifier underscores
        punct_to_strip = ''.join([c for c in string.punctuation if c not in ('_', '=', '>', '<', '!')])
        cleaned = cleaned.translate(str.maketrans('', '', punct_to_strip))
        # Remove extra spaces
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        # 2. Replacements
        for pattern, replacement in self.replacements.items():
            cleaned = re.sub(pattern, replacement, cleaned)
            
        context.normalized_query = cleaned
        
        # 3. Detect operations
        ops = set()
        words = cleaned.split()
        for word in words:
            for op, keywords in self.operation_keywords.items():
                if word in keywords:
                    ops.add(op)
        context.operations = list(ops)
        
        # 4. Extract possible columns (if dataset_profile is available)
        possible_cols = set()
        dp = context.dataset_profile
        if dp and isinstance(dp, dict):
            cols = dp.get('column_types') or dp.get('columns')
            # columns could be a list of dicts or list of strings
            col_names = []
            if isinstance(cols, list):
                if len(cols) > 0 and isinstance(cols[0], dict):
                    col_names = [str(c.get('name', '')).lower() for c in cols]
                elif len(cols) > 0 and isinstance(cols[0], str):
                    col_names = [c.lower() for c in cols]
            
            # Simple matching: if any word in the query matches a column name
            for word in words:
                for cname in col_names:
                    if word == cname or word in cname.split('_') or word in cname.split(' '):
                        possible_cols.add(cname)
        context.possible_columns = list(possible_cols)
        
        # 5. Ambiguity score
        if cleaned in self.vague_queries or len(words) < 2:
            context.ambiguity_score = 1.0
        else:
            context.ambiguity_score = 0.1

query_normalizer = QueryNormalizer()

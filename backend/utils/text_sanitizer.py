"""
Plexis Text Sanitizer — Strict Elimination of Chain-of-Thought and Prompt Leakage

Guarantees that NO internal reasoning, thinking process markers, system prompt leakage,
or agent execution traces ever reach user-facing outputs across any engine.
"""
from __future__ import annotations
import re
import logging

logger = logging.getLogger(__name__)

# Fenced or tagged reasoning blocks
_TAGGED_REASONING_PATTERNS = [
    re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<thought>.*?</thought>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<reasoning>.*?</reasoning>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<analysis>.*?</analysis>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<scratchpad>.*?</scratchpad>', re.DOTALL | re.IGNORECASE),
    re.compile(r'\[THINKING\].*?\[/THINKING\]', re.DOTALL | re.IGNORECASE),
    re.compile(r'\[REASONING\].*?\[/REASONING\]', re.DOTALL | re.IGNORECASE),
]

# Prefixes and section headers that signal natural-language chain-of-thought
_COT_HEADER_PATTERN = re.compile(
    r"^(?:\s*(?:\d+[\.\)]\s*)?\*{0,2}(?:"
    r"Identify Constraints(?:\s*&\s*Rules)?"
    r"|Determine Response Strategy"
    r"|Draft Response(?:\s*\(.*?\))?"
    r"|Understand the Human"
    r"|Read the Social Intent"
    r"|Analyze User Input"
    r"|Analyze the Request"
    r"|Thinking Process"
    r"|Thought Process"
    r"|Internal Reasoning"
    r"|Conversational Reasoning"
    r"|Response Behaviour"
    r"|Response Plan"
    r"|Constraints\s*&\s*Rules"
    r"|Here(?:'s| is) (?:a |the |my )?(?:thinking|thought) process"
    r"|Let's analyze the (?:user|request|input)"
    r")\*{0,2}:?)",
    re.IGNORECASE | re.MULTILINE
)

# Individual lines that recite internal developer instructions, policies, or prompt markers
_SYSTEM_LEAK_LINES = re.compile(
    r"^(?:.*(?:"
    r"CURRENT INTENT:"
    r"|DATASET LOADED:"
    r"|DATASET STATE:"
    r"|SYSTEM NOTE:"
    r"|The system note says"
    r"|According to the system instructions"
    r"|The system prompt states"
    r"|Rules say [\"“]"
    r"|Key: Never invent data"
    r"|CRITICAL STRICT DIRECTIVE"
    r").*)$",
    re.IGNORECASE | re.MULTILINE
)

# Bullet points reciting system instructions or reasoning self-talk
_LEAKAGE_BULLET_PATTERN = re.compile(
    r"^\s*[-*•]\s*(?:"
    r"I am Plexis.*"
    r"|No dataset is currently loaded.*"
    r"|I must not invent.*"
    r"|I should not output.*"
    r"|If asked about underlying model.*"
    r"|I need to handle the .* situation.*"
    r"|User asks which .*"
    r"|The user didn't ask about .*"
    r"|Acknowledge the question.*"
    r"|Explain that no dataset is currently loaded.*"
    r"|Offer to help if they upload.*"
    r"|Keep it warm, conversational.*"
    r"|No emojis forced.*"
    r"|Actually, rules:.*"
    r"|Response: Acknowledge, explain.*"
    r"|Warm, Plexis style.*"
    r")$",
    re.IGNORECASE | re.MULTILINE
)


def is_internal_planning_dump(text: str) -> bool:
    """
    Check if a text block contains recognizable internal planning, CoT, or system prompt leakage.
    Uses structural signals and specific internal phrases to avoid false positives on legitimate prose.
    """
    if not text or not isinstance(text, str):
        return False

    lowered = text.lower()
    leak_indicators = [
        "identify constraints & rules",
        "determine response strategy",
        "draft response (internal reasoning)",
        "i am plexis, ai data-analysis assistant",
        "i must not invent column names",
        "i should not output python code",
        "i should not output chain-of-thought",
        "here's a thinking process:",
        "analyze user input:",
        "rules say \"most friendly/conversational",
        "the user didn't ask about that; they asked about",
        "this response is powered by model 'nvidia/nemotron",
    ]
    if any(ind in lowered for ind in leak_indicators):
        return True

    # Check for structural CoT: A CoT header combined with multiple internal bullet points
    if _COT_HEADER_PATTERN.search(text) and len(re.findall(r'^\s*[-*•]', text, re.MULTILINE)) >= 3:
        return True

    return False


def strip_thinking_and_internal_reasoning(text: str) -> str:
    """
    Remove any chain-of-thought, reasoning steps, or prompt leakage from model output.
    Returns clean user-facing prose.
    """
    if not text or not isinstance(text, str):
        return ""

    cleaned = text

    # 1. Strip explicit tagged blocks
    for pat in _TAGGED_REASONING_PATTERNS:
        cleaned = pat.sub('', cleaned)

    # 1b. Strip unclosed opening thinking tags if present
    cleaned = re.sub(r'<(?:think|thought|reasoning|analysis|scratchpad)>.*$', '', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # 2. Check for natural language thinking process headers
    m = _COT_HEADER_PATTERN.search(cleaned)
    if m:
        logger.warning("[Sanitizer] Detected natural-language chain-of-thought header. Stripping internal reasoning.")
        sub_text = cleaned[m.start():]

        # Look for explicit response headers or quoted draft
        draft_match = re.search(r'Something like:\s*["“](.*?)["”]', sub_text, re.DOTALL | re.IGNORECASE)
        resp_match = re.search(
            r'(?:(?:\n---|---\n|\n\*\*\*\n)'
            r'|\n(?:Final Answer|Response|Direct Response|Conclusion):\s*'
            r'|\n\n(?!(?:[0-9]+\.|\*|-)\s+)(?=[A-Z]))',
            sub_text
        )
        if resp_match:
            candidate = sub_text[resp_match.end():].strip()
            if candidate and not is_internal_planning_dump(candidate):
                cleaned = candidate
            else:
                cleaned = ""
        elif draft_match:
            candidate = draft_match.group(1).strip()
            if candidate and not is_internal_planning_dump(candidate):
                cleaned = candidate
            else:
                cleaned = ""
        else:
            # Check individual paragraphs and remove contaminated blocks
            paras = [p.strip() for p in cleaned.split('\n\n') if p.strip()]
            clean_paras = []
            for p in paras:
                if not _COT_HEADER_PATTERN.search(p) and not is_internal_planning_dump(p):
                    clean_paras.append(p)
            cleaned = '\n\n'.join(clean_paras)

    # 3. Strip individual prompt leakage lines and bullets
    cleaned = _SYSTEM_LEAK_LINES.sub('', cleaned)
    cleaned = _LEAKAGE_BULLET_PATTERN.sub('', cleaned)

    # 4. Clean up excessive whitespace/newlines
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    # 5. Unicode character cleanup (consistent with conversation engine)
    cleaned = (
        cleaned
        .replace('\u2011', '-')
        .replace('\u2013', '-')
        .replace('\u2014', '-')
        .replace('\u202f', ' ')
        .replace('\u00a0', ' ')
    )

    return cleaned

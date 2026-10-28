"""
Plexis Response Planner

This module implements the Response Planning layer for the Conversation Engine.

WHAT IT DOES:
Before the LLM generates any response, the planner analyzes the incoming message
and conversation context to produce a ConversationPlan — a structured internal
description of how the response should be shaped.

WHAT IT IS NOT:
- Not a hardcoded length rule ("greetings = 2 sentences")
- Not a prompt template
- Not a routing decision
- Not a formatting rule

WHAT IT IS:
A reusable, intelligence-driven planning stage that understands conversational
rhythm. It answers the question: "How much conversation does this moment deserve?"

The plan is used ONLY internally — it guides the LLM's response generation
but is never shown to the user.

ARCHITECTURE:
The planner uses a fast heuristic classifier (no extra LLM call, no latency)
built on signal analysis:
- Linguistic signals (message length, question marks, keywords)
- Emotional signals (excitement, frustration, celebration, confusion)
- Conversational signals (sharing vs. asking vs. reacting)
- Context signals (conversation depth, topic shifts)
- Complexity signals (technical terms, multi-part questions)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


# ---------------------------------------------------------------------------
# Response Length Categories
# ---------------------------------------------------------------------------

class ResponseLength:
    """
    Semantic length categories for conversational responses.
    These are NOT sentence counts. They are conversational depth signals.
    """
    TINY      = "tiny"      # Acknowledgements, reactions, yes/no, "ok", "thanks"
    SHORT     = "short"     # Greetings, casual, compliments, quick opinions
    MEDIUM    = "medium"    # Achievements, recommendations, friendly discussion
    LONG      = "long"      # Explanations, teaching, comparisons, technical depth
    ADAPTIVE  = "adaptive"  # Emotional, storytelling, context-driven — let the LLM decide


# ---------------------------------------------------------------------------
# Conversation Plan
# ---------------------------------------------------------------------------

@dataclass
class ConversationPlan:
    """
    The internal plan produced by the Response Planner.

    This object captures everything the LLM needs to understand about
    HOW to respond — not WHAT to say (that's the LLM's job).

    Fields:
        intent:           What the user is trying to do or communicate
        emotion:          Detected emotional state of the message
        user_energy:      How engaged/excited the user seems
        response_length:  The ResponseLength category that fits this moment
        should_ask_followup: Whether a follow-up question would add value
        emoji_level:      none | subtle | warm (contextual emoji guidance)
        tone:             casual | warm | encouraging | analytical | teaching | celebratory
        conversation_goal: What the ideal response should accomplish
        depth_signals:    Raw signals that influenced this plan (for debugging)
    """
    intent: str
    emotion: str
    user_energy: str
    response_length: str
    should_ask_followup: bool
    emoji_level: str
    tone: str
    conversation_goal: str
    depth_signals: Dict[str, Any] = field(default_factory=dict)

    def to_guidance(self) -> str:
        """
        Render the plan as natural language guidance for the LLM.
        This is injected into the system prompt — not shown to the user.
        """
        emoji_guide = {
            "none":   "No emojis in this response.",
            "subtle": "One emoji is appropriate if it fits naturally.",
            "warm":   "One or two emojis would feel natural and genuine here.",
        }.get(self.emoji_level, "Use emojis sparingly.")

        followup_guide = (
            "End with one genuine follow-up question if it flows naturally."
            if self.should_ask_followup
            else "Do not end with a question — an observation or statement is stronger here."
        )

        length_guide = {
            ResponseLength.TINY: (
                "This moment calls for a very brief response — one sentence, maybe two. "
                "Do not over-explain. Do not pad. Just respond to the moment."
            ),
            ResponseLength.SHORT: (
                "Keep this concise and warm. A few sentences is all this needs. "
                "Don't overload the conversation with more than is natural."
            ),
            ResponseLength.MEDIUM: (
                "This deserves a real response — engaged, present, conversational. "
                "Not a quick brush-off, but also not an essay. Match the energy of the moment."
            ),
            ResponseLength.LONG: (
                "This calls for depth. The user needs a thorough, thoughtful response. "
                "Explain fully, don't skip important details, use structure where it helps. "
                "Quality and completeness matter more than brevity here."
            ),
            ResponseLength.ADAPTIVE: (
                "Let the emotional context guide your response length. "
                "Be as long or as short as the conversation naturally needs. "
                "Follow the human rhythm of what's being shared."
            ),
        }.get(self.response_length, "")

        return (
            f"RESPONSE GUIDANCE:\n"
            f"Detected intent: {self.intent}\n"
            f"Emotional tone: {self.emotion}\n"
            f"User energy: {self.user_energy}\n"
            f"Response tone: {self.tone}\n"
            f"Conversation goal: {self.conversation_goal}\n\n"
            f"LENGTH GUIDANCE: {length_guide}\n\n"
            f"EMOJI GUIDANCE: {emoji_guide}\n\n"
            f"FOLLOW-UP GUIDANCE: {followup_guide}"
        )


# ---------------------------------------------------------------------------
# Signal Analysis Helpers
# ---------------------------------------------------------------------------

# Tiny signal patterns — very short reactions with no information need
_TINY_PATTERNS = re.compile(
    r'^(ok|okay|ok+|k|yep|yeah|yup|sure|cool|nice|got it|'
    r'thanks|thank you|thx|ty|cheers|np|no problem|'
    r'lol|lmao|haha|hehe|😂|💀|yes|no|nope|nah|'
    r'alright|right|understood|noted|sounds good|perfect|great|awesome)\W*$',
    re.IGNORECASE
)

# Short signal patterns — casual / social / compliment
_SHORT_PATTERNS = re.compile(
    r'\b(hi|hello|hey|sup|yo|howdy|good morning|good evening|'
    r'how are you|how.s it going|what.s up|'
    r'you.re (cute|amazing|great|awesome|smart|the best)|'
    r'i like you|love you|you.re so|you are so)\b',
    re.IGNORECASE
)

# Celebration / achievement signals
_CELEBRATION_PATTERNS = re.compile(
    r'\b(finally|fixed it|got it working|it works|works now|'
    r'i did it|we did it|completed|finished|solved|nailed it|'
    r'made it|shipped|deployed|passed|won|achieved|figured it out|'
    r'breakthrough|progress|milestone)\b',
    re.IGNORECASE
)

# Excitement amplifiers (all caps, exclamation streaks)
_HIGH_ENERGY = re.compile(r'[A-Z]{4,}|!{2,}|[a-z]{2,}o{2,}|bro{2,}')

# Teaching / explanation signals
_TEACHING_PATTERNS = re.compile(
    r'\b(explain|how does|how do|what is|what are|what.s|why is|why does|'
    r'teach me|help me understand|what.s the difference|'
    r'define|definition|overview|introduction|beginner|basics|'
    r'walk me through|step by step|tutorial)\b',
    re.IGNORECASE
)

# Comparison signals
_COMPARISON_PATTERNS = re.compile(
    r'\b(vs|versus|compare|comparison|difference between|better than|'
    r'which is better|pros and cons|trade.?offs?|alternatives?)\b',
    re.IGNORECASE
)

# Technical / deep-dive signals
_TECHNICAL_PATTERNS = re.compile(
    r'\b(debug|error|bug|fix|refactor|optimize|architecture|'
    r'algorithm|complexity|performance|deploy|pipeline|'
    r'sql|api|database|schema|query|code|function|class|'
    r'machine learning|neural network|transformer|regression|'
    r'pandas|polars|numpy|tensorflow|pytorch|docker|kubernetes|'
    r'implement|integration|endpoint|microservice)\b',
    re.IGNORECASE
)

# Frustration / struggle signals
_FRUSTRATION_PATTERNS = re.compile(
    r'\b(don.t understand|confused|confusing|lost|stuck|'
    r'can.t figure out|doesn.t work|not working|broken|'
    r'frustrated|help|ugh|why is this|so hard|this is hard|'
    r'what am i doing wrong|nothing works|i give up|struggling)\b',
    re.IGNORECASE
)

# Multi-part question signal
_MULTI_QUESTION = re.compile(r'\?.*\?', re.DOTALL)

# Open-ended sharing signal (user sharing something about their life/work)
_SHARING_PATTERNS = re.compile(
    r'^i (just|recently|finally|started|bought|built|made|'
    r'learned|discovered|realized|found out|got|am|have been|'
    r'was|did|created|finished|launched)\b',
    re.IGNORECASE
)

# Emotional context patterns
_EMOTIONAL_PATTERNS = re.compile(
    r'\b(feeling|feel|felt|scared|nervous|anxious|excited|happy|'
    r'sad|worried|stressed|overwhelmed|proud|grateful|'
    r'disappointing|hope|hopeful|lonely|confused|lost)\b',
    re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Response Planner
# ---------------------------------------------------------------------------

class ResponsePlanner:
    """
    Analyzes a conversation message and context to produce a ConversationPlan.

    The planner uses linguistic, emotional, and contextual signal analysis
    to determine the ideal shape of a response — before the LLM generates it.

    This is a heuristic classifier: fast, synchronous, zero extra LLM calls.
    It is designed to be accurate for common conversational patterns and
    gracefully falls back to ADAPTIVE when signals are ambiguous.
    """

    def plan(
        self,
        message: str,
        intent: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        has_dataset: bool = False,
    ) -> ConversationPlan:
        """
        Produce a ConversationPlan for the given conversational moment.

        Args:
            message:              The current user message (normalized)
            intent:               The classified intent
            conversation_history: Recent conversation turns
            has_dataset:          Whether a dataset is loaded

        Returns:
            A ConversationPlan describing how the response should be shaped.
        """
        history = conversation_history or []
        msg = message.strip()
        msg_lower = msg.lower()
        msg_len = len(msg.split())

        # --- Collect signals ---
        signals = self._collect_signals(msg, msg_lower, msg_len, history, intent, has_dataset)

        # --- Determine response shape ---
        length      = self._determine_length(signals)
        tone        = self._determine_tone(signals)
        emotion     = self._determine_emotion(signals)
        user_energy = self._determine_energy(signals)
        emoji_level = self._determine_emoji(signals, length)
        followup    = self._should_ask_followup(signals, length)
        goal        = self._determine_goal(signals, length, tone)

        return ConversationPlan(
            intent=intent,
            emotion=emotion,
            user_energy=user_energy,
            response_length=length,
            should_ask_followup=followup,
            emoji_level=emoji_level,
            tone=tone,
            conversation_goal=goal,
            depth_signals=signals,
        )

    # -----------------------------------------------------------------------
    # Signal Collection
    # -----------------------------------------------------------------------

    def _collect_signals(
        self,
        msg: str,
        msg_lower: str,
        msg_len: int,
        history: List[Dict],
        intent: str,
        has_dataset: bool,
    ) -> Dict[str, Any]:
        """Extract all relevant signals from the message and context."""
        return {
            # Structural
            "msg_len":           msg_len,
            "has_question_mark": "?" in msg,
            "has_multi_question": bool(_MULTI_QUESTION.search(msg)),
            "is_very_short":     msg_len <= 3,
            "is_short":          msg_len <= 8,
            "is_medium":         8 < msg_len <= 25,
            "is_long_message":   msg_len > 25,

            # Pattern matches
            "is_tiny_reaction":   bool(_TINY_PATTERNS.match(msg_lower)),
            "is_greeting":        bool(_SHORT_PATTERNS.search(msg_lower)) and msg_len <= 8,
            "is_celebration":     bool(_CELEBRATION_PATTERNS.search(msg_lower)),
            "is_high_energy":     bool(_HIGH_ENERGY.search(msg)),
            "is_teaching_req":    bool(_TEACHING_PATTERNS.search(msg_lower)),
            "is_comparison":      bool(_COMPARISON_PATTERNS.search(msg_lower)),
            "is_technical":       bool(_TECHNICAL_PATTERNS.search(msg_lower)),
            "is_frustrated":      bool(_FRUSTRATION_PATTERNS.search(msg_lower)),
            "is_sharing":         bool(_SHARING_PATTERNS.match(msg_lower)),
            "is_emotional":       bool(_EMOTIONAL_PATTERNS.search(msg_lower)),

            # Context
            "intent":             intent,
            "has_dataset":        has_dataset,
            "history_depth":      len(history),
            "is_repeat_message":  self._is_repeat(msg_lower, history),
            "last_was_question":  self._last_plexis_was_question(history),
        }

    def _is_repeat(self, msg_lower: str, history: List[Dict]) -> bool:
        """Check if the user is saying the same thing they said recently."""
        for turn in history[-4:]:
            if turn.get('role') == 'user':
                if turn.get('content', '').lower().strip() == msg_lower:
                    return True
        return False

    def _last_plexis_was_question(self, history: List[Dict]) -> bool:
        """Check if Plexis's last message ended with a question."""
        for turn in reversed(history):
            if turn.get('role') in ('assistant', 'plexis'):
                return '?' in turn.get('content', '')
        return False

    # -----------------------------------------------------------------------
    # Decision Logic
    # -----------------------------------------------------------------------

    def _determine_length(self, s: Dict) -> str:
        """Determine the ideal response length from signals."""

        # Absolute tiny: pure reaction words
        if s["is_tiny_reaction"]:
            return ResponseLength.TINY

        # Teaching / comparison / technical → always Long
        if s["is_teaching_req"] or s["is_comparison"]:
            return ResponseLength.LONG

        # Celebration / achievement → Medium (celebrate + one followup)
        if s["is_celebration"]:
            return ResponseLength.MEDIUM

        if s["is_technical"] and (s["has_question_mark"] or s["is_medium"] or s["is_long_message"]):
            return ResponseLength.LONG

        # Greeting without context → Short
        if s["is_greeting"] and not s["is_technical"] and not s["has_question_mark"]:
            return ResponseLength.SHORT

        # Simple very short non-reactive message → Short
        if s["is_very_short"] and not s["is_teaching_req"] and not s["is_technical"]:
            return ResponseLength.SHORT

        # User sharing something personal/interesting → Medium
        if s["is_sharing"] and not s["is_technical"]:
            return ResponseLength.MEDIUM

        # Emotional conversation → Adaptive
        if s["is_emotional"] or s["is_frustrated"]:
            return ResponseLength.ADAPTIVE

        # Multi-part question → Long
        if s["has_multi_question"]:
            return ResponseLength.LONG

        # Single direct question (short message) → Short to Medium based on complexity
        if s["has_question_mark"] and s["is_short"]:
            return ResponseLength.SHORT

        if s["has_question_mark"] and s["is_medium"]:
            return ResponseLength.MEDIUM

        # Long message from user (they put effort in) → Medium to Long
        if s["is_long_message"]:
            return ResponseLength.MEDIUM

        # Default: let context lead
        return ResponseLength.ADAPTIVE

    def _determine_tone(self, s: Dict) -> str:
        if s["is_frustrated"]:
            return "encouraging"
        if s["is_celebration"] or s["is_high_energy"]:
            return "celebratory"
        if s["is_emotional"]:
            return "warm"
        if s["is_teaching_req"] or s["is_comparison"]:
            return "teaching"
        if s["is_technical"]:
            return "analytical"
        if s["is_greeting"] or s["is_tiny_reaction"]:
            return "casual"
        if s["is_sharing"]:
            return "curious"
        return "warm"

    def _determine_emotion(self, s: Dict) -> str:
        if s["is_frustrated"]:
            return "frustrated or confused"
        if s["is_celebration"] or s["is_high_energy"]:
            return "excited and celebratory"
        if s["is_emotional"]:
            return "emotionally engaged"
        if s["is_sharing"]:
            return "sharing and open"
        if s["is_greeting"]:
            return "neutral and friendly"
        if s["is_tiny_reaction"]:
            return "brief reaction"
        return "neutral"

    def _determine_energy(self, s: Dict) -> str:
        if s["is_high_energy"] or s["is_celebration"]:
            return "high"
        if s["is_frustrated"] or s["is_emotional"]:
            return "emotionally invested"
        if s["is_greeting"] or s["is_tiny_reaction"]:
            return "low"
        if s["is_teaching_req"] or s["is_technical"]:
            return "focused and curious"
        return "moderate"

    def _determine_emoji(self, s: Dict, length: str) -> str:
        # Analytical and technical → no emojis
        if s["is_technical"] or s["is_teaching_req"] or s["is_comparison"]:
            return "none"
        # Analytical length → no emojis
        if length == ResponseLength.LONG:
            return "subtle"
        # Celebrations → warm
        if s["is_celebration"] or s["is_high_energy"]:
            return "warm"
        # Greetings, sharing → warm
        if s["is_greeting"] or s["is_sharing"]:
            return "warm"
        # Frustration → subtle (supportive but not over-the-top)
        if s["is_frustrated"]:
            return "subtle"
        # Tiny reactions → none (don't add emojis to "ok")
        if s["is_tiny_reaction"]:
            return "none"
        return "subtle"

    def _should_ask_followup(self, s: Dict, length: str) -> bool:
        # Never ask a question if they just answered one (avoid question ping-pong)
        if s["last_was_question"] and s["is_tiny_reaction"]:
            return False
        # Always engage with sharing and celebrations
        if s["is_celebration"] or s["is_sharing"]:
            return True
        # Teaching rarely needs follow-ups — the user just wants to learn
        if s["is_teaching_req"] and not s["has_multi_question"]:
            return False
        # Greetings often benefit from a warm follow-up
        if s["is_greeting"]:
            return True
        # Short messages: follow-up can open the conversation
        if s["is_short"] and not s["is_tiny_reaction"]:
            return True
        # Frustration: ask clarifying questions to help
        if s["is_frustrated"]:
            return True
        # Long technical question: answer first, follow-up is optional
        if length == ResponseLength.LONG:
            return False
        return False

    def _determine_goal(self, s: Dict, length: str, tone: str) -> str:
        if s["is_celebration"]:
            return "Celebrate the achievement genuinely and invite them to share more"
        if s["is_frustrated"]:
            return "Make them feel supported and help them get unstuck"
        if s["is_teaching_req"]:
            return "Explain clearly and make the concept genuinely understandable"
        if s["is_comparison"]:
            return "Give a thorough, balanced comparison that helps them decide"
        if s["is_technical"]:
            return "Provide technically accurate, precise help"
        if s["is_sharing"]:
            return "Engage with what they shared and show genuine curiosity"
        if s["is_greeting"]:
            return "Welcome them warmly and open up the conversation"
        if s["is_emotional"]:
            return "Respond to the emotion first, then the content"
        if s["is_tiny_reaction"]:
            return "Acknowledge naturally and keep the conversation moving"
        return "Respond authentically and add value to the conversation"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

response_planner = ResponsePlanner()

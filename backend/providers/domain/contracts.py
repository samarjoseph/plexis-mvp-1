from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field

class ProviderCapabilities(BaseModel):
    """
    Explicitly defines the exact capabilities a model or provider supports.
    """
    vision: bool = False
    embeddings: bool = False
    parallel_tool_calling: bool = False
    structured_output: bool = False
    temperature_support: bool = True
    multimodal: bool = False
    streaming: bool = False
    json_mode: bool = False
    reasoning_level: str = "none" # none, basic, advanced
    max_input_tokens: int = 8192
    max_output_tokens: int = 4096
    latency_profile: str = "medium" # low, medium, high
    cost_profile: str = "medium" # low, medium, high

class AIRequest(BaseModel):
    """
    A fully versioned contract mandating SLAs for routing execution.
    """
    contract_version: str = "1.0"
    
    # Task definition
    task: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    system_prompt: Optional[str] = None
    
    # Functional constraints
    streaming: bool = False
    json_mode: bool = False
    priority: Literal["LOW", "NORMAL", "HIGH", "CRITICAL"] = "NORMAL"
    
    # SLAs
    quality_target: Literal["FAST", "BALANCED", "HIGH_QUALITY"] = "BALANCED"
    latency_target_ms: Optional[int] = None
    cost_budget: Optional[float] = None
    reasoning_depth: Optional[str] = None
    response_style: Optional[str] = None
    
    # Optional parameters
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    model_id: Optional[str] = None
    user_plan: Optional[str] = "free"

class IProviderAdapter:
    """
    Anti-Corruption Layer (ACL) Interface for Providers.
    Every external API (Gemini, Groq) must be translated into this interface.
    """
    def generate(self, request: AIRequest) -> Any:
        raise NotImplementedError

    def generate_stream(self, request: AIRequest) -> Any:
        raise NotImplementedError

    def get_capabilities(self, model_name: str) -> ProviderCapabilities:
        raise NotImplementedError

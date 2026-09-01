"""Static DeepSeek prompt and canonical response-schema identity."""

import json
from typing import Annotated

from pydantic import Field, TypeAdapter

from researchos.domain.agent import AgentFailedDecision, AgentFinalDecision
from researchos.domain.identity import sha256_text
from researchos.domain.planning import CandidatePlan
from researchos.domain.synthesis import (
    BlueResponse,
    JudgeResponse,
    RedResponse,
    SynthesisCandidate,
)

Phase9ARealAgentDecision = Annotated[
    AgentFinalDecision | AgentFailedDecision, Field(discriminator="kind")
]

DEEPSEEK_PROMPTS = {
    "planning": "Return one strict ResearchOS PlanningModelResponse payload as JSON.",
    "agent": "Return one strict ResearchOS AgentDecision as JSON.",
    "verification": (
        "Return the requested strict ResearchOS verification role payload as JSON."
    ),
}
DEEPSEEK_RESPONSE_CONTRACTS = {
    "planning": "planning-model-response-v1",
    "agent": "agent-direct-decision-v1",
    "verification": "verification-model-response-v1",
}
DEEPSEEK_RESPONSE_SCHEMAS = {
    "planning": CandidatePlan.model_json_schema(),
    "agent": TypeAdapter(Phase9ARealAgentDecision).json_schema(),
    "verification": {
        "synthesizer": SynthesisCandidate.model_json_schema(),
        "red": RedResponse.model_json_schema(),
        "blue": BlueResponse.model_json_schema(),
        "judge": JudgeResponse.model_json_schema(),
    },
}


def deepseek_system_prompt(role_id: str) -> str:
    try:
        prompt = DEEPSEEK_PROMPTS[role_id]
        schema = DEEPSEEK_RESPONSE_SCHEMAS[role_id]
    except KeyError as exc:
        raise ValueError("unsupported DeepSeek model role") from exc
    canonical_schema = json.dumps(
        schema,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{prompt}\nCanonical response schema:\n{canonical_schema}"


def deepseek_prompt_content_hash(role_id: str) -> str:
    return sha256_text(deepseek_system_prompt(role_id))

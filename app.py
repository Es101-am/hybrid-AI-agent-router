import os
import sys
import json
import logging
from typing import Optional, Tuple, Dict, Any, List

import yaml
from pydantic import BaseModel, Field, ValidationError, root_validator
from openai import OpenAI

# ----------------------------
# Config models & loader
# ----------------------------

class AppCfg(BaseModel):
    name: str = "HybridRouter"
    model: str = "gpt-4o"
    tau: float = Field(0.85, ge=0.0, le=1.0)
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    clarify_on_low_conf: bool = True

class HybridCfg(BaseModel):
    enabled: bool = True
    max_loops: int = Field(2, ge=0)
    planner_budget: int = Field(3, ge=1)
    critic_threshold: float = Field(0.6, ge=0.0, le=1.0)
    ask_before_side_effects: bool = True

class OpenAICfg(BaseModel):
    api_key: Optional[str] = None

class LogCfg(BaseModel):
    level: str = "INFO"

class Cfg(BaseModel):
    app: AppCfg = AppCfg()
    hybrid: HybridCfg = HybridCfg()
    openai: OpenAICfg = OpenAICfg()
    logging: LogCfg = LogCfg()

def load_cfg(path: str = "config.yaml") -> Cfg:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Cfg(**raw)

# ----------------------------
# OpenAI client factory
# ----------------------------

def make_client(cfg: Cfg) -> OpenAI:
    api_key = cfg.openai.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI API key not found. Set OPENAI_API_KEY or put it in config.yaml.")
    return OpenAI(api_key=api_key)

# ----------------------------
# Types
# ----------------------------

try:
    from typing import Literal, Dict, Any
except ImportError:
    from typing_extensions import Literal  # type: ignore
    from typing import Dict, Any

class IntentClassification(BaseModel):
    intent: Literal["question", "request", "complaint"]
    confidence: float
    reasoning: str
    needs_multi_step: bool = False  # ok to keep default here (already working)

# Every field required (no defaults)
class PlanStep(BaseModel):
    tool: Literal["answer_handler", "request_handler", "complaint_handler", "none"]
    args: Dict[str, Any]               
    reason: str                        
    confidence: float                  # we still enforce bounds in prompt

class CriticOutput(BaseModel):
    approve: bool                      # 
    score: float                       # 
    feedback: str                      # 
    request_revision: bool             # 

# ----------------------------
# Core: classify
# ----------------------------

def classify_intent(client: OpenAI, cfg: Cfg, user_input: str) -> IntentClassification:
    system_msg = (
        "Classify the user's message as one of: question, request, complaint.\n"
        "Return fields: intent, confidence ∈ [0,1], reasoning, needs_multi_step (bool).\n"
        "Set needs_multi_step=true if resolving likely requires more than one action, "
        "disambiguation, policy lookup, or escalation."
    )
    resp = client.responses.parse(
        model=cfg.app.model,
        input=[{"role": "system", "content": system_msg},
               {"role": "user", "content": user_input}],
        text_format=IntentClassification,
        temperature=cfg.app.temperature,
    )
    return resp.output_parsed

# ----------------------------
# Deterministic handlers (tools)
# ----------------------------

def answer_question(client: OpenAI, cfg: Cfg, question: str) -> str:
    r = client.responses.create(
        model=cfg.app.model,
        input=f"Answer concisely:\n\n{question}",
        temperature=0.2,
    )
    return getattr(r, "output_text", r.output[0].content[0].text)

def process_request(request: str) -> str:
    return f"Processing your request: {request}"

def handle_complaint(complaint: str) -> str:
    return f"I understand your concern about: {complaint}. I've logged this and will escalate."

def ask_for_clarification(user_input: str) -> str:
    return (
        "I’m not fully confident I understood. Could you clarify whether this is a "
        "question, a request to perform an action, or a complaint? "
        f"(Your message was: “{user_input}”)"
    )

# ----------------------------
# Tool registry & executor
# ----------------------------

def execute_tool(
    tool_name: str, args: Dict[str, Any], client: OpenAI, cfg: Cfg, user_input: str
) -> str:
    if tool_name == "answer_handler":
        question = args.get("question") or user_input
        return answer_question(client, cfg, question)
    if tool_name == "request_handler":
        req = args.get("request") or user_input
        return process_request(req)
    if tool_name == "complaint_handler":
        comp = args.get("complaint") or user_input
        return handle_complaint(comp)
    if tool_name == "none":
        return "No tool required; responding directly."
    return "Unknown tool."

# ----------------------------
# Planner / Critic / Safety
# ----------------------------
import json
import re

def parse_json_object(text: str) -> dict:
    """
    Extract the first JSON object from a string and parse it.
    Robust to extra text or code fences.
    """
    # Try fenced ```json blocks first
    fence = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return json.loads(fence.group(1))
    # Fallback: first {...} object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end+1])
    raise ValueError(f"Could not find JSON object in: {text[:200]}...")

import json

def plan_next_step(client: OpenAI, cfg: Cfg, user_input: str, history: List[str]) -> PlanStep:
    tool_manifest = (
        "Available tools:\n"
        "- answer_handler(question: string)\n"
        "- request_handler(request: string)\n"
        "- complaint_handler(complaint: string)\n"
        "Choose EXACTLY ONE tool or 'none'."
    )
    system_prompt = (
        "You are a cautious planner that selects ONE next tool call.\n"
        f"Budget remaining steps: {cfg.hybrid.planner_budget}.\n"
        "Return ONLY a minified JSON object on a single line, with ALL and ONLY these keys:\n"
        '{"tool": "...", "args": {...}, "reason": "...", "confidence": 0.0}\n'
        "Where tool ∈ ['answer_handler','request_handler','complaint_handler','none'] and confidence ∈ [0,1].\n"
        "Do NOT include any explanation before or after the JSON."
    )
    user_content = (
        f"{tool_manifest}\n\n"
        f"User message: {user_input}\n"
        f"Interaction summary so far: {history[-3:]}"
    )

    resp = client.responses.create(
        model=cfg.app.model,
        input=[{"role": "system", "content": system_prompt},
               {"role": "user", "content": user_content}],
        temperature=0.2,
    )

    raw = getattr(resp, "output_text", resp.output[0].content[0].text)
    data = parse_json_object(raw)
    return PlanStep(**data)




def critic_check(client: OpenAI, cfg: Cfg, user_input: str, last_output: str) -> CriticOutput:
    system = (
        "You are a critic verifying whether the latest tool result resolves the user's need.\n"
        f"Approve if score >= {cfg.hybrid.critic_threshold}; else set request_revision=true.\n"
        "Return ONLY a minified JSON object on a single line, with ALL and ONLY these keys:\n"
        '{"approve": true, "score": 0.0, "feedback": "...", "request_revision": false}\n'
        "Do NOT include any explanation before or after the JSON."
    )
    user = (
        f"User message: {user_input}\n"
        f"Latest result: {last_output}\n"
        f"Threshold: {cfg.hybrid.critic_threshold}"
    )

    resp = client.responses.create(
        model=cfg.app.model,
        input=[{"role": "system", "content": system},
               {"role": "user", "content": user}],
        temperature=0.0,
    )

    raw = getattr(resp, "output_text", resp.output[0].content[0].text)
    data = parse_json_object(raw)
    return CriticOutput(**data)



def safety_guard(cfg: Cfg, text: str) -> Tuple[bool, str]:
    """
    Minimal placeholder. In production hook PII redaction, policy checks, etc.
    Returns (ok, maybe_redacted_text).
    """
    banned = []  # add patterns if needed
    for token in banned:
        if token in text:
            return False, "Content blocked by safety policy."
    return True, text

# ----------------------------
# Hybrid routing
# ----------------------------

def deterministic_route(client: OpenAI, cfg: Cfg, ic: IntentClassification, user_input: str) -> str:
    if ic.intent == "question":
        return answer_question(client, cfg, user_input)
    if ic.intent == "request":
        return process_request(user_input)
    if ic.intent == "complaint":
        return handle_complaint(user_input)
    return "I'm not sure how to help with that."

def hybrid_route(client: OpenAI, cfg: Cfg, user_input: str) -> Tuple[str, IntentClassification]:
    ic = classify_intent(client, cfg, user_input)
    logging.info("Classified intent=%s conf=%.2f multi=%s | %s",
                 ic.intent, ic.confidence, ic.needs_multi_step, ic.reasoning)

    # Fast deterministic gate
    if ic.confidence >= cfg.app.tau and not ic.needs_multi_step:
        result = deterministic_route(client, cfg, ic, user_input)
        ok, out = safety_guard(cfg, result)
        if not ok:
            # fallback to deterministic handler again (no-op) or escalate
            result = "Unable to deliver response due to safety policy."
        return result, ic

    # Otherwise use agentic loop
    if not cfg.hybrid.enabled:
        # Optional clarification if hybrid disabled
        if cfg.app.clarify_on_low_conf:
            return ask_for_clarification(user_input), ic
        return deterministic_route(client, cfg, ic, user_input), ic

    history: List[str] = []
    last_output = ""
    loops = 0

    while loops < cfg.hybrid.max_loops:
        loops += 1
        step = plan_next_step(client, cfg, user_input, history)
        logging.info("Planner chose tool=%s conf=%.2f reason=%s args=%s",
                     step.tool, step.confidence, step.reason, step.args)

        if step.tool == "none":
            # fall back to deterministic with original intent
            last_output = deterministic_route(client, cfg, ic, user_input)
        else:
            last_output = execute_tool(step.tool, step.args, client, cfg, user_input)

        history.append(f"tool={step.tool} -> {last_output[:120]}")

        critic = critic_check(client, cfg, user_input, last_output)
        logging.info("Critic approve=%s score=%.2f feedback=%s",
                     critic.approve, critic.score, critic.feedback)

        if critic.approve and critic.score >= cfg.hybrid.critic_threshold:
            ok, redacted = safety_guard(cfg, last_output)
            if not ok:
                # fallback deterministic route if safety fails
                return deterministic_route(client, cfg, ic, user_input), ic
            return redacted, ic

        # otherwise loop for a revision
        if loops >= cfg.hybrid.max_loops:
            break

    # Fallback router (deterministic backstop)
    result = deterministic_route(client, cfg, ic, user_input)
    return result, ic

# ----------------------------
# CLI demo
# ----------------------------

def main():
    cfg = load_cfg()
    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    client = make_client(cfg)

    examples = [
        "What is machine learning?",
        "Please schedule a meeting for tomorrow 10am.",
        "I'm unhappy with the service quality lately.",
        "I was double-charged, can you refund and also explain my plan?",
        "Cancel it."  # ambiguous → likely planner path
    ]

    for text in examples:
        print("\nInput:", text)
        resp, cls = hybrid_route(client, cfg, text)
        print("Intent:", cls.intent, "| Conf:", round(cls.confidence, 3), "| Multi:", cls.needs_multi_step)
        print("Reasoning:", cls.reasoning)
        print("Response:", resp)

if __name__ == "__main__":
    main()

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple, Callable, Any

from openai import OpenAI

from .config import AgentConfig
from .prompt import SYSTEM_PROMPT
from .tools import build_tools
from ..config import get_openai_api_key, Settings


ToolFunc = Callable[..., Any]


class AgentRunner:
    def __init__(self, cfg: AgentConfig, api_key: Optional[str] = None) -> None:
        key = get_openai_api_key(api_key)
        self.client = OpenAI(api_key=key)
        self.cfg = cfg
        self.tools: Dict[str, Tuple[ToolFunc, dict]]
        self.tools, self.tool_specs = build_tools(cfg.db_path, cfg.schema_path)

    def ask(self, question: str, model: Optional[str] = None, max_turns: int = 8) -> dict:
        messages: List[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        used_tools: List[dict] = []
        mdl = model or self.cfg.model or Settings.from_env().openai_model

        for _ in range(max_turns):
            resp = self.client.chat.completions.create(
                model=mdl,
                messages=messages,
                tools=self.tool_specs,
                temperature=0.1,
            )
            choice = resp.choices[0]
            msg = choice.message

            if msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                })

                for call in msg.tool_calls:
                    name = call.function.name
                    args = json.loads(call.function.arguments or "{}")
                    func = self.tools[name][0]
                    try:
                        result = func(**args)
                        result_json = json.dumps(result, ensure_ascii=False)[:16000]
                        used_tools.append({"name": name, "args": args})
                        messages.append({"role": "tool", "tool_call_id": call.id, "name": name, "content": result_json})
                    except Exception as e:
                        err = f"Tool '{name}' error: {e}"
                        messages.append({"role": "tool", "tool_call_id": call.id, "name": name, "content": json.dumps({"error": err})})
                continue

            answer = (msg.content or "").strip()
            return {"answer": answer, "used_tools": used_tools}

        return {"answer": "Reached tool-use limit without a final answer.", "used_tools": used_tools}


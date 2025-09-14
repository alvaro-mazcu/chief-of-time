from __future__ import annotations

import json
from pathlib import Path
from typing import List, Dict

from openai import OpenAI


def transcribe_with_whisper(audio_path: Path, api_key: str) -> str:
    """Transcribe an audio file using OpenAI Whisper and return plain text."""
    client = OpenAI(api_key=api_key)
    with Path(audio_path).open("rb") as f:
        tr = client.audio.transcriptions.create(model="whisper-1", file=f)
    return (tr.text or "").strip()


PLAN_SYSTEM_PROMPT = (
    "You convert spoken notes into a daily plan as JSON. "
    "Return ONLY a JSON array (no outer key) of todo items. "
    "Each item: {id: string, title: string, completed: boolean, priority: 'high'|'medium'|'low'}. "
    "Infer priorities; default completed=false. No extra commentary."
)


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # remove first line fence
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def _extract_json_array(text: str) -> str:
    l = text.find("[")
    r = text.rfind("]")
    return text[l : r + 1] if l != -1 and r != -1 and r > l else text


def _normalize_plan_items(items) -> List[Dict]:
    import uuid

    norm: List[Dict] = []
    if isinstance(items, dict):
        items = [items]
    elif not isinstance(items, list):
        items = [str(items)]

    for it in items:
        if isinstance(it, str):
            obj = {"id": uuid.uuid4().hex[:8], "title": it, "completed": False, "priority": "medium"}
        elif isinstance(it, dict):
            obj = dict(it)
            obj.setdefault("id", uuid.uuid4().hex[:8])
            obj.setdefault("title", obj.get("task") or obj.get("name") or "Untitled")
            comp_val = obj.get("completed", False)
            if isinstance(comp_val, str):
                obj["completed"] = comp_val.lower() in ("true", "yes", "done")
            else:
                obj["completed"] = bool(comp_val)
            pr = str(obj.get("priority", "medium")).lower()
            if pr not in ("high", "medium", "low"):
                pr = "medium"
            obj["priority"] = pr
        else:
            obj = {"id": uuid.uuid4().hex[:8], "title": str(it), "completed": False, "priority": "medium"}
        norm.append(
            {
                "id": str(obj.get("id")),
                "title": str(obj.get("title")),
                "completed": bool(obj.get("completed", False)),
                "priority": str(obj.get("priority", "medium")).lower(),
            }
        )
    return norm


def transcript_to_daily_plan(transcript: str, api_key: str, model: str) -> List[Dict]:
    """Turn transcript text into a normalized list of todo dicts via Chat Completions."""
    client = OpenAI(api_key=api_key)
    comp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        temperature=0.2,
    )
    raw = (comp.choices[0].message.content or "").strip()
    content = _strip_code_fences(raw)
    content = _extract_json_array(content)
    data = json.loads(content)
    return _normalize_plan_items(data)


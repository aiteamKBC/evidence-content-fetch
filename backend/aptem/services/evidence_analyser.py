import json
import logging
import re

import requests
from django.conf import settings


logger = logging.getLogger(__name__)

DEFAULT_ANALYSIS = {
    "analysis_status": "pending",
    "has_reflection": False,
    "reflection_strength": "none",
    "reflection_evidence": [],
    "has_ksbs": False,
    "has_explicit_ksbs": False,
    "has_implicit_ksbs": False,
    "ksb_detection_type": "none",
    "ksb_codes": [],
    "knowledge_found": False,
    "skills_found": False,
    "behaviours_found": False,
    "ksb_evidence": [],
    "analysis_summary": "",
}

ALLOWED_REFLECTION_STRENGTHS = {"none", "weak", "moderate", "strong"}
ALLOWED_KSB_DETECTION_TYPES = {"none", "explicit", "implicit", "explicit_and_implicit"}
KSB_CODE_RE = re.compile(r"\b([KSB]\d{1,3})\b", re.I)


def analyse_evidence(text):
    if not text or not text.strip():
        result = DEFAULT_ANALYSIS.copy()
        result["analysis_status"] = "skipped"
        result["analysis_summary"] = "No extracted text was available for analysis."
        return result

    api_key = settings.OPENAI_API_KEY
    model = getattr(settings, "OPENAI_MODEL", "")
    if not api_key or not model:
        return DEFAULT_ANALYSIS.copy()

    try:
        return _analyse_with_openai(text, api_key, model)
    except Exception as exc:
        logger.warning("Evidence analysis failed: %s", exc)
        result = DEFAULT_ANALYSIS.copy()
        result["analysis_status"] = "failed"
        result["analysis_summary"] = "AI analysis failed; download and word count were completed."
        return result


def _analyse_with_openai(text, api_key, model):
    prompt = (
        "Apply this review only when the extracted evidence text is under 1000 words. "
        "For under-1000-word evidence, assess whether the learner explains what they learned, "
        "not just what they did. A description of tasks, activities, steps, responsibilities, "
        "meetings, documents, or completed work is not enough on its own. Meaningful "
        "learning/reflection is present only when the learner clearly explains one or more "
        "of: what they learned, what they now understand better, how their thinking changed, "
        "what they would improve next time, what worked or did not work and why, or how the "
        "experience affected their skills, knowledge, behaviour, or future practice. If the "
        "learner only describes actions completed, with no clear learning or reflective "
        "insight, mark reflection as false. Also check whether the learner explicitly "
        "identifies the KSBs achieved. Explicit KSBs require written references to codes "
        "such as K1, K2, S1, S2, B1, B2, or clear wording that specific KSBs were met. Do "
        "not invent KSB codes. If no KSBs are explicitly mentioned, mark explicit KSBs as "
        "false. Return only compact valid JSON with exactly these keys: has_reflection, "
        "reflection_strength, reflection_evidence, has_ksbs, has_explicit_ksbs, "
        "has_implicit_ksbs, ksb_detection_type, ksb_codes, knowledge_found, skills_found, "
        "behaviours_found, ksb_evidence, analysis_summary. Use booleans for all boolean "
        "fields. Use reflection_strength as one of: none, weak, moderate, strong. Use "
        "ksb_detection_type as one of: none, explicit, implicit, explicit_and_implicit. "
        "Keep reflection_evidence and ksb_evidence as short direct quotes, maximum five each."
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a strict apprenticeship evidence quality auditor. Your role is "
                    "to assess short learner evidence under 1000 words and identify whether "
                    "it contains genuine learning reflection and explicitly identified KSB "
                    "achievement. Be conservative and evidence-led. Do not give credit for "
                    "headings, templates, copied criteria, task instructions, generic "
                    "descriptions, or lists of activities. A learner saying what they did is "
                    "not enough. The learner must explain what they learned, understood, "
                    "improved, would change, or how the experience affected their future "
                    "practice. For KSBs, only treat them as explicitly identified when the "
                    "learner names specific KSB codes such as K1, K2, S1, S2, B1, B2, or "
                    "clearly states which KSBs were achieved. Do not invent KSB codes and do "
                    "not infer explicit KSB achievement from general task descriptions. If "
                    "the evidence is ambiguous, vague, template-like, or only describes "
                    "activity without learning, mark reflection as false. Return valid JSON "
                    "only, with no commentary outside the JSON object."
                ),
            },
            {
                "role": "user",
                "content": f"{prompt}\n\nEvidence text:\n{text[:60000]}",
            },
        ],
        "text": {"format": {"type": "json_object"}},
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    raw_text = _response_text(data)
    parsed = json.loads(raw_text)
    return normalize_analysis(parsed, text)


def _response_text(data):
    if data.get("output_text"):
        return data["output_text"]
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return content["text"]
    raise ValueError("OpenAI response did not contain JSON text.")


def normalize_analysis(data, source_text=""):
    result = DEFAULT_ANALYSIS.copy()
    result["analysis_status"] = "complete"
    result.update({key: data.get(key, result[key]) for key in result if key in data})

    for key in [
        "has_reflection",
        "has_ksbs",
        "has_explicit_ksbs",
        "has_implicit_ksbs",
        "knowledge_found",
        "skills_found",
        "behaviours_found",
    ]:
        result[key] = coerce_bool(result.get(key))

    if result["reflection_strength"] not in ALLOWED_REFLECTION_STRENGTHS:
        result["reflection_strength"] = "none"
    if result["ksb_detection_type"] not in ALLOWED_KSB_DETECTION_TYPES:
        result["ksb_detection_type"] = "none"

    explicit_codes = sorted({match.group(1).upper() for match in KSB_CODE_RE.finditer(source_text)})
    submitted_codes = [str(code).upper() for code in coerce_list(result.get("ksb_codes"))]
    result["ksb_codes"] = [code for code in submitted_codes if code in explicit_codes]
    result["has_explicit_ksbs"] = bool(result["ksb_codes"]) and bool(result["has_explicit_ksbs"])
    result["reflection_evidence"] = coerce_list(result.get("reflection_evidence"))[:5]
    result["ksb_evidence"] = coerce_list(result.get("ksb_evidence"))[:5]
    result["analysis_summary"] = str(result.get("analysis_summary") or "")[:1000]

    has_any_ksb = bool(
        result["has_explicit_ksbs"]
        or result["has_implicit_ksbs"]
        or result["knowledge_found"]
        or result["skills_found"]
        or result["behaviours_found"]
    )
    result["has_ksbs"] = bool(result["has_ksbs"] or has_any_ksb)
    if result["has_explicit_ksbs"] and result["has_implicit_ksbs"]:
        result["ksb_detection_type"] = "explicit_and_implicit"
    elif result["has_explicit_ksbs"]:
        result["ksb_detection_type"] = "explicit"
    elif result["has_implicit_ksbs"]:
        result["ksb_detection_type"] = "implicit"
    elif not result["has_ksbs"]:
        result["ksb_detection_type"] = "none"

    return result


def coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0", "none", ""}:
            return False
    return False


def coerce_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, tuple):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(value)]

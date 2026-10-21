"""Incident retrospective agent.

Reads an incident write-up and produces, in three steps:
  1. a structured timeline
  2. the contributing factors (root cause analysis)
  3. an impact summary and proposed action items

Usage:
    python agent.py [path/to/incident.txt]
"""

import argparse
import json
import os

from dotenv import load_dotenv
import aisuite as ai

load_dotenv()  # reads the API key from .env (never printed)

MODEL = "anthropic:claude-haiku-5-5"
MAX_TOKENS = 2000
DEFAULT_INPUT = "data/gitlab_timeline.txt"

client = ai.Client()


# ---------- Helpers ----------

def ask_llm(prompt):
    """Send one prompt to the model and return its text reply."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS,
        # aisuite only reads the first text block, so turn off "thinking" blocks
        thinking={"type": "disabled"},
    )
    return response.choices[0].message.content


def parse_json(text):
    """Parse JSON from the model, removing ```json fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]      # drop the opening ``` line
        text = text.rsplit("```", 1)[0]    # drop the closing ```
    return json.loads(text)


def run_step(name, prompt, validate):
    """Ask the model, parse and validate the JSON. Retry once on failure."""
    reply = ask_llm(prompt)
    try:
        data = parse_json(reply)
        validate(data)
        return data
    except (json.JSONDecodeError, ValueError) as error:
        problem = str(error)
        print(f"  {name}: validation failed ({problem}). Retrying once...")

    retry_prompt = (
        prompt
        + "\n\nYour previous answer was invalid because: " + problem
        + "\nReturn ONLY valid JSON in the exact format requested."
    )
    reply = ask_llm(retry_prompt)
    data = parse_json(reply)  # raises if still invalid
    validate(data)
    return data


def require_text_fields(item, fields, label):
    """Raise ValueError unless every field is a non-empty string."""
    if not isinstance(item, dict):
        raise ValueError(f"{label} is not an object")
    for field in fields:
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is missing '{field}'")


# ---------- Validators ----------

def validate_timeline(data):
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list) or not events:
        raise ValueError("'events' must be a non-empty list")
    for i, event in enumerate(events):
        require_text_fields(event, ["time", "description"], f"event {i}")


def validate_factors(data):
    factors = data.get("factors") if isinstance(data, dict) else None
    if not isinstance(factors, list) or not factors:
        raise ValueError("'factors' must be a non-empty list")
    for i, factor in enumerate(factors):
        require_text_fields(factor, ["factor", "evidence"], f"factor {i}")


def validate_actions(data):
    if not isinstance(data, dict):
        raise ValueError("answer must be a JSON object")
    summary = data.get("impact_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("'impact_summary' must be a non-empty string")
    items = data.get("action_items")
    if not isinstance(items, list) or not items:
        raise ValueError("'action_items' must be a non-empty list")
    for i, item in enumerate(items):
        label = f"action item {i}"
        require_text_fields(item, ["action", "owner_role", "type", "priority"], label)
        if item["type"] not in ("prevent", "detect", "mitigate"):
            raise ValueError(f"{label} has invalid type '{item['type']}'")
        if item["priority"] not in ("high", "medium", "low"):
            raise ValueError(f"{label} has invalid priority '{item['priority']}'")


# ---------- Steps ----------

def extract_timeline(incident_text):
    prompt = f"""You are an experienced site reliability engineer writing an incident retrospective.

Read the incident report below and extract the timeline of key events in chronological order.
Use the times exactly as they appear in the report (include dates or time zones if given).
If an event has no exact time, use the closest description of when it happened (for example "shortly after").

Return ONLY JSON in this format, with no other text:
{{"events": [{{"time": "...", "description": "..."}}]}}

INCIDENT REPORT:
{incident_text}"""
    return run_step("Step 1", prompt, validate_timeline)


def find_contributing_factors(incident_text, timeline):
    prompt = f"""You are an experienced site reliability engineer writing a blameless incident retrospective.

Using the incident report and the extracted timeline below, identify the contributing factors
that caused the incident or made it worse. Incidents usually have more than one contributing factor,
so consider technical, process, tooling, and communication factors.
Focus on systems and processes, not on blaming individuals.

For each factor, give evidence: a short quote or close paraphrase from the incident report.
Only use evidence that is actually in the report.

Return ONLY JSON in this format, with no other text:
{{"factors": [{{"factor": "...", "evidence": "..."}}]}}

TIMELINE:
{json.dumps(timeline, indent=2)}

INCIDENT REPORT:
{incident_text}"""
    return run_step("Step 2", prompt, validate_factors)


def draft_impact_and_actions(incident_text, timeline, factors):
    prompt = f"""You are an experienced site reliability engineer writing a blameless incident retrospective.

Using the incident report, timeline, and contributing factors below:
1. Write a short impact summary (2-4 sentences): who or what was affected, how badly, and for how long.
   Only state facts supported by the report.
2. Propose action items that address the contributing factors.

Rules for each action item:
- "action": a specific, concrete change.
- "owner_role": a team or role (for example "Infrastructure team" or "On-call engineer").
  Never use a person's name.
- "type": exactly one of "prevent", "detect", "mitigate".
- "priority": exactly one of "high", "medium", "low".

Return ONLY JSON in this format, with no other text:
{{"impact_summary": "...",
  "action_items": [{{"action": "...", "owner_role": "...", "type": "...", "priority": "..."}}]}}

TIMELINE:
{json.dumps(timeline, indent=2)}

CONTRIBUTING FACTORS:
{json.dumps(factors, indent=2)}

INCIDENT REPORT:
{incident_text}"""
    return run_step("Step 3", prompt, validate_actions)


# ---------- Output ----------

def save_json(data, folder, filename):
    path = os.path.join(folder, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved {path}")


def print_summary(timeline, factors, actions):
    events = timeline["events"]
    print("\n========== INCIDENT RETROSPECTIVE ==========")

    print(f"\nTIMELINE ({len(events)} events)")
    print(f"  First: {events[0]['time']} - {events[0]['description']}")
    print(f"  Last:  {events[-1]['time']} - {events[-1]['description']}")

    print("\nCONTRIBUTING FACTORS")
    for factor in factors["factors"]:
        print(f"  - {factor['factor']}")

    print("\nIMPACT")
    print(f"  {actions['impact_summary']}")

    print("\nACTION ITEMS")
    for item in actions["action_items"]:
        print(f"  [{item['priority']}/{item['type']}] {item['action']} -- {item['owner_role']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Incident retrospective agent")
    parser.add_argument("input_file", nargs="?", default=DEFAULT_INPUT,
                        help=f"incident text file (default: {DEFAULT_INPUT})")
    args = parser.parse_args()

    with open(args.input_file, encoding="utf-8") as f:
        incident_text = f.read()

    # outputs/<input file name without extension>/
    name = os.path.splitext(os.path.basename(args.input_file))[0]
    out_folder = os.path.join("outputs", name)
    os.makedirs(out_folder, exist_ok=True)

    print("Step 1: extracting timeline...")
    timeline = extract_timeline(incident_text)
    save_json(timeline, out_folder, "step1.json")

    print("Step 2: identifying contributing factors...")
    factors = find_contributing_factors(incident_text, timeline)
    save_json(factors, out_folder, "step2.json")

    print("Step 3: drafting impact summary and action items...")
    actions = draft_impact_and_actions(incident_text, timeline, factors)
    save_json(actions, out_folder, "step3.json")

    print_summary(timeline, factors, actions)


if __name__ == "__main__":
    main()

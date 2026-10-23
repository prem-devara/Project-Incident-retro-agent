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
import re

from dotenv import load_dotenv
import aisuite as ai

load_dotenv()  # reads the API key from .env (never printed)

MODEL = "anthropic:claude-haiku-5-5"         # Steps 1-3
REVIEW_MODEL = "anthropic:claude-sonnet-5-5"  # Step 4 (review)
# Each model has its own setting for "no thinking before the answer"
THINKING_OFF = {
    MODEL: {"type": "disabled"},
    REVIEW_MODEL: {"type": "between_tools"},
}
MAX_TOKENS = 2000
REVIEW_MAX_TOKENS = 4000  # Step 4 rewrites factors + action items, so it needs more room
DEFAULT_INPUT = "data/gitlab_timeline.txt"

client = ai.Client()


# ---------- Helpers ----------

def ask_llm(prompt, max_tokens=MAX_TOKENS, model=MODEL):
    """Send one prompt to the model and return its text reply."""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        # aisuite only reads the first text block, so turn off "thinking" blocks
        thinking=THINKING_OFF[model],
    )
    return response.choices[0].message.content


def parse_json(text):
    """Parse JSON from the model, removing ```json fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]      # drop the opening ``` line
        text = text.rsplit("```", 1)[0]    # drop the closing ```
    return json.loads(text)


def run_step(name, prompt, validate, max_tokens=MAX_TOKENS, model=MODEL):
    """Ask the model, parse and validate the JSON. Retry once on failure."""
    reply = ask_llm(prompt, max_tokens, model)
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
    reply = ask_llm(retry_prompt, max_tokens, model)
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
    validate_action_items(data.get("action_items"))


def validate_action_items(items):
    if not isinstance(items, list) or not items:
        raise ValueError("'action_items' must be a non-empty list")
    for i, item in enumerate(items):
        label = f"action item {i}"
        require_text_fields(item, ["action", "owner_role", "type", "priority"], label)
        if item["type"] not in ("prevent", "detect", "mitigate"):
            raise ValueError(f"{label} has invalid type '{item['type']}'")
        if item["priority"] not in ("high", "medium", "low"):
            raise ValueError(f"{label} has invalid priority '{item['priority']}'")


def merge_target(verdict):
    """Return N for a "merge with #N" verdict, otherwise None."""
    match = re.fullmatch(r"merge with #(\d+)", verdict.strip().lower())
    return int(match.group(1)) if match else None


def validate_review(data, factor_count):
    if not isinstance(data, dict):
        raise ValueError("answer must be a JSON object")

    # Exactly one verdict per factor, numbered 1..factor_count
    verdicts = data.get("factor_verdicts")
    if not isinstance(verdicts, list):
        raise ValueError("'factor_verdicts' must be a list")
    by_id = {}
    for i, v in enumerate(verdicts):
        require_text_fields(v, ["verdict", "reason"], f"verdict {i}")
        if not isinstance(v.get("id"), int) or not 1 <= v["id"] <= factor_count:
            raise ValueError(f"verdict {i} has an invalid 'id'")
        if v["id"] in by_id:
            raise ValueError(f"factor #{v['id']} has more than one verdict")
        by_id[v["id"]] = v
    if len(by_id) != factor_count:
        raise ValueError(f"expected a verdict for each of the {factor_count} factors")

    for n, v in by_id.items():
        verdict = v["verdict"].strip().lower()
        if verdict in ("keep", "remove"):
            continue
        target = merge_target(verdict)
        if target is None:
            raise ValueError(f"factor #{n} has invalid verdict '{v['verdict']}'")
        if target == n or target not in by_id:
            raise ValueError(f"factor #{n} merges into an invalid factor #{target}")
        if by_id[target]["verdict"].strip().lower() != "keep":
            raise ValueError(f"factor #{n} merges into #{target}, which is not kept")
    if not any(v["verdict"].strip().lower() == "keep" for v in verdicts):
        raise ValueError("at least one factor must be kept")

    validate_action_items(data.get("action_items"))
    changes = data.get("action_changes")
    if not isinstance(changes, list):
        raise ValueError("'action_changes' must be a list")
    for i, change in enumerate(changes):
        require_text_fields(change, ["change", "reason"], f"action change {i}")


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


def review_and_revise(incident_text, factors, actions):
    factor_list = factors["factors"]
    numbered = "\n".join(
        f"#{n}. {f['factor']}\n    Evidence: {f['evidence']}"
        for n, f in enumerate(factor_list, start=1)
    )
    prompt = f"""You are a senior incident reviewer checking a draft blameless incident retrospective.

Review the numbered draft contributing factors and the draft action items below against the incident report.

PART A - give a verdict for EVERY factor, using these checks:
1. The factor must have contributed to the failure or slowed recovery.
   If it actually helped recovery or limited the damage, its verdict is "remove".
2. The factor's evidence must actually support its claim, using what is in the report.
   If it is speculation, its verdict is "remove".
3. No two factors may describe the same underlying issue. For duplicates, keep the best one
   and give the others the verdict "merge with #N", where #N is the factor you keep.
   You may only merge into a factor whose verdict is "keep".
4. Otherwise the verdict is "keep".
5. No wording may blame an individual. If a kept factor blames an individual, or should be
   reworded to cover factors merged into it, put the improved text in "new_wording".
   Otherwise leave "new_wording" out.
Each verdict needs a one-line "reason".

PART B - revise the action items:
- Each action item must be specific, have an owner role (a team or role, never a person's name),
  and be verifiable as done. Rewrite vague items.
- Remove or rewrite action items that only address factors you removed.
- No wording may blame an individual.
- Keep action items that pass these checks unchanged.
- "type" must be exactly one of "prevent", "detect", "mitigate";
  "priority" must be exactly one of "high", "medium", "low".
- List every action item you removed or edited in "action_changes", each with a one-line reason.
  Only list changes you actually made; use an empty list if there are none.

Return ONLY JSON in this format, with no other text:
{{"factor_verdicts": [{{"id": 1, "verdict": "keep | remove | merge with #N", "reason": "...", "new_wording": "optional"}}],
  "action_items": [{{"action": "...", "owner_role": "...", "type": "...", "priority": "..."}}],
  "action_changes": [{{"change": "...", "reason": "..."}}]}}

DRAFT CONTRIBUTING FACTORS:
{numbered}

DRAFT ACTION ITEMS:
{json.dumps(actions["action_items"], indent=2)}

INCIDENT REPORT:
{incident_text}"""
    review = run_step(
        "Step 4", prompt,
        lambda data: validate_review(data, len(factor_list)),
        REVIEW_MAX_TOKENS, REVIEW_MODEL,
    )

    # Build the revised factors and the changes log in code from the verdicts
    revised, factor_changes = apply_verdicts(factor_list, review["factor_verdicts"])
    return {
        "factor_verdicts": review["factor_verdicts"],
        "factors": revised,
        "action_items": review["action_items"],
        "changes": factor_changes + review["action_changes"],
    }


def apply_verdicts(factor_list, verdicts):
    """Apply keep / remove / merge verdicts. Returns (revised factors, changes)."""
    by_id = {v["id"]: v for v in verdicts}

    # Which factors get merged into each kept factor
    merged_into = {}
    for v in verdicts:
        target = merge_target(v["verdict"])
        if target:
            merged_into.setdefault(target, []).append(v["id"])

    revised = []
    changes = []
    for n, factor in enumerate(factor_list, start=1):
        verdict = by_id[n]
        decision = verdict["verdict"].strip().lower()
        target = merge_target(decision)

        if decision == "remove":
            changes.append({"change": f"Removed #{n}: {factor['factor']}",
                            "reason": verdict["reason"]})
        elif target:
            changes.append({"change": f"Merged #{n} into #{target}: {factor['factor']}",
                            "reason": verdict["reason"]})
        else:  # keep
            new_factor = dict(factor)
            # Combine evidence from any factors merged into this one
            for source in merged_into.get(n, []):
                new_factor["evidence"] += " | " + factor_list[source - 1]["evidence"]
            new_wording = (verdict.get("new_wording") or "").strip()
            if new_wording and new_wording != factor["factor"]:
                new_factor["factor"] = new_wording
                changes.append({"change": f"Reworded #{n}: {new_wording}",
                                "reason": verdict["reason"]})
            revised.append(new_factor)

    return revised, changes


# ---------- Output ----------

def save_json(data, folder, filename):
    path = os.path.join(folder, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved {path}")


def print_summary(timeline, factors, actions, review):
    events = timeline["events"]
    print("\n========== INCIDENT RETROSPECTIVE ==========")

    print("\nMODELS")
    print(f"  Steps 1-3 (timeline, factors, actions): {MODEL}")
    print(f"  Step 4 (review):                         {REVIEW_MODEL}")

    print(f"\nTIMELINE ({len(events)} events)")
    print(f"  First: {events[0]['time']} - {events[0]['description']}")
    print(f"  Last:  {events[-1]['time']} - {events[-1]['description']}")

    # Show the revised (Step 4) lists; the drafts stay in step2/step3.json
    before = len(factors["factors"])
    after = len(review["factors"])
    print(f"\nCONTRIBUTING FACTORS ({before} before review -> {after} after)")
    for factor in review["factors"]:
        print(f"  - {factor['factor']}")

    print("\nIMPACT")
    print(f"  {actions['impact_summary']}")

    print("\nACTION ITEMS")
    for item in review["action_items"]:
        print(f"  [{item['priority']}/{item['type']}] {item['action']} -- {item['owner_role']}")

    print(f"\nREVIEW CHANGES ({len(review['changes'])})")
    if not review["changes"]:
        print("  (none)")
    for change in review["changes"]:
        print(f"  - {change['change']} -- {change['reason']}")
    print()


def run_agent(input_path, output_dir=None):
    """Run steps 1-4 on one incident file and save step1-4.json.

    output_dir defaults to outputs/<input file name without extension>/.
    Returns the four step results.
    """
    with open(input_path, encoding="utf-8") as f:
        incident_text = f.read()

    if output_dir is None:
        name = os.path.splitext(os.path.basename(input_path))[0]
        output_dir = os.path.join("outputs", name)
    out_folder = output_dir
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

    print("Step 4: reviewing and revising the draft...")
    review = review_and_revise(incident_text, factors, actions)
    save_json(review, out_folder, "step4.json")

    print_summary(timeline, factors, actions, review)
    return {"timeline": timeline, "factors": factors,
            "actions": actions, "review": review}


def main():
    parser = argparse.ArgumentParser(description="Incident retrospective agent")
    parser.add_argument("input_file", nargs="?", default=DEFAULT_INPUT,
                        help=f"incident text file (default: {DEFAULT_INPUT})")
    args = parser.parse_args()
    run_agent(args.input_file)


if __name__ == "__main__":
    main()

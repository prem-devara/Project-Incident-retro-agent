"""Score the agent's contributing factors against an answer key.

Runs the full agent (steps 1-4) several times, then uses an LLM judge to
label each final factor as match / valid_extra / duplicate / wrong.
Recall and precision are computed in code from those labels.

Usage:
    python evaluate.py data/gitlab_timeline.txt eval/gitlab_answer_key.txt
    python evaluate.py data/gitlab_timeline.txt eval/gitlab_answer_key.txt --rescore
    python evaluate.py data/gitlab_timeline.txt eval/gitlab_answer_key.txt --judge-test
"""

import argparse
import json
import os

import agent

NUM_RUNS = 5
JUDGE_MODEL = agent.REVIEW_MODEL  # same model and thinking setting as Step 4
LABELS = ("match", "valid_extra", "duplicate", "wrong")
EXCLUDED_MARKER = "(not in input)"

# --judge-test: known-bad factors appended to run_1's real factors
JUDGE_TEST_RUNS = 3
PLANTED_FACTORS = [
    {"id": "P1", "expected": "wrong",
     "factor": "The manual LVM snapshot taken 6 hours earlier caused the data loss.",
     "evidence": "the LVM snapshot created 6 hours before the outage"},
    {"id": "P2", "expected": "wrong",
     "factor": "An Azure region outage caused the replication lag.",
     "evidence": "replication process started to lag behind"},
    {"id": "P3", "expected": "wrong",
     "factor": "Spam emails rejected by DMARC caused the database overload.",
     "evidence": "DMARC was not enabled for the cronjob emails"},
    {"id": "P4", "expected": "wrong",
     "factor": "Engineers delayed the response by waiting for replies on Slack.",
     "evidence": "asked for help on Slack"},
    {"id": "P5", "expected": "duplicate",
     "factor": "A production data directory was deleted because the engineer believed "
               "they were working on the secondary server.",
     "evidence": "errantly thinking they were doing so on the secondary"},
]


# ---------- Answer key ----------

def load_key_factors(key_path):
    """Return the CONTRIBUTING FACTORS lines as [{"id", "text", "excluded"}]."""
    with open(key_path, encoding="utf-8") as f:
        lines = [line.strip() for line in f]

    factors = []
    in_section = False
    for line in lines:
        if line == "CONTRIBUTING FACTORS":
            in_section = True
        elif in_section and line.startswith("- "):
            text = line[2:].strip()
            factors.append({
                "id": len(factors) + 1,
                "text": text.replace(EXCLUDED_MARKER, "").strip(),
                "excluded": EXCLUDED_MARKER in text,
            })
        elif in_section and line:
            break  # next section header
    if not factors:
        raise ValueError(f"no CONTRIBUTING FACTORS found in {key_path}")
    return factors


# ---------- Judge ----------

def validate_judgement(data, factor_count, key_count):
    if not isinstance(data, dict):
        raise ValueError("answer must be a JSON object")
    labels = data.get("labels")
    if not isinstance(labels, list):
        raise ValueError("'labels' must be a list")

    seen = set()
    for i, item in enumerate(labels):
        agent.require_text_fields(item, ["label", "reason"], f"label {i}")
        n = item.get("id")
        if not isinstance(n, int) or not 1 <= n <= factor_count:
            raise ValueError(f"label {i} has an invalid 'id'")
        if n in seen:
            raise ValueError(f"factor #{n} has more than one label")
        seen.add(n)

        label = item["label"]
        if label not in LABELS:
            raise ValueError(f"factor #{n} has invalid label '{label}'")
        if label == "match":
            key_ids = item.get("key_ids")
            if (not isinstance(key_ids, list) or not key_ids
                    or len(set(key_ids)) != len(key_ids)
                    or not all(isinstance(k, int) and 1 <= k <= key_count for k in key_ids)):
                raise ValueError(
                    f"factor #{n} is a match but 'key_ids' must be a non-empty list "
                    f"of distinct answer-key numbers")
        if label == "duplicate":
            target = item.get("duplicate_of")
            if not isinstance(target, int) or not 1 <= target < n:
                raise ValueError(
                    f"factor #{n} is a duplicate but 'duplicate_of' must be an earlier factor")
    if len(seen) != factor_count:
        raise ValueError(f"expected a label for each of the {factor_count} factors")


def judge_factors(incident_text, key_factors, agent_factors):
    key_list = "\n".join(
        f"K{k['id']}. {k['text']}" + (f" {EXCLUDED_MARKER}" if k["excluded"] else "")
        for k in key_factors
    )
    agent_list = "\n".join(
        f"#{n}. {f['factor']}\n    Evidence: {f['evidence']}"
        for n, f in enumerate(agent_factors, start=1)
    )
    prompt = f"""You are grading an incident retrospective against an answer key.

Below are the incident report, the answer key's contributing factors (K1, K2, ...), and the
contributing factors written by the agent being graded (#1, #2, ...).

Give EVERY agent factor exactly one label:
- "match": it describes the same cause (mechanism) as an answer-key factor, even if it covers
  only part of that factor, adds extra detail, or uses completely different wording.
  Judge by mechanism, not wording. Give the matched factor numbers as a "key_ids" list
  (for K3, use [3]). If one agent factor combines causes from several answer-key factors
  (for example, bad data AND missing validation), list every one it covers, e.g. [1, 3].
- "valid_extra": it is not in the answer key, but the incident report supports it as something
  that caused the incident or slowed recovery.
- "duplicate": it repeats the same issue as an EARLIER agent factor.
  Give that factor's number as "duplicate_of". Label the earlier factor on its own merits.
- "wrong": the incident report does not support it, or it gets the cause backwards.
  Any factor that presents something which reduced harm or enabled recovery as if it were
  a cause is "wrong" (for example, a snapshot or backup that limited data loss).
Each label needs a one-sentence "reason".

Return ONLY JSON in this format, with no other text:
{{"labels": [{{"id": 1, "label": "match | valid_extra | duplicate | wrong", "key_ids": [3], "duplicate_of": 1, "reason": "..."}}]}}
Include "key_ids" only for "match" and "duplicate_of" only for "duplicate".

ANSWER KEY CONTRIBUTING FACTORS:
{key_list}

AGENT CONTRIBUTING FACTORS:
{agent_list}

INCIDENT REPORT:
{incident_text}"""
    data = agent.run_step(
        "Judge", prompt,
        lambda d: validate_judgement(d, len(agent_factors), len(key_factors)),
        agent.REVIEW_MAX_TOKENS, JUDGE_MODEL,
    )
    return sorted(data["labels"], key=lambda item: item["id"])


# ---------- Scoring ----------

def score_run(labels, key_factors, agent_factors):
    """Compute recall and precision in code from the judge's labels."""
    key_by_id = {k["id"]: k for k in key_factors}
    counted_keys = [k["id"] for k in key_factors if not k["excluded"]]

    results = []
    for item, factor in zip(labels, agent_factors):
        result = {"id": item["id"], "factor": factor["factor"],
                  "label": item["label"], "reason": item["reason"]}
        if item["label"] == "match":
            keys = [key_by_id[k] for k in item["key_ids"]]
            result["key_ids"] = [k["id"] for k in keys]
            result["key_texts"] = [k["text"] for k in keys]
            result["key_excluded"] = [k["excluded"] for k in keys]
        if item["label"] == "duplicate":
            result["duplicate_of"] = item["duplicate_of"]
        results.append(result)

    counts = {label: sum(r["label"] == label for r in results) for label in LABELS}
    matched = {k for r in results if r["label"] == "match" for k in r["key_ids"]}
    recalled = [k for k in counted_keys if k in matched]

    return {
        "labels": results,
        "counts": counts,
        "total_factors": len(results),
        "recall": len(recalled) / len(counted_keys) if counted_keys else None,
        "precision": (counts["match"] + counts["valid_extra"]) / len(results),
        "missed_key_factors": [key_by_id[k]["text"] for k in counted_keys
                               if k not in matched],
    }


def summarize(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}


def fmt(value):
    return "-" if value is None else f"{value:.2f}"


def print_table(runs, summary):
    print("\n========== EVALUATION ==========")
    print(f"{'run':<5}{'factors':>8}{'match':>7}{'extra':>7}{'dup':>5}"
          f"{'wrong':>7}{'recall':>8}{'precision':>11}")
    for run in runs:
        if "error" in run:
            print(f"{run['run']:<5}  ERROR: {run['error']}")
            continue
        c = run["counts"]
        print(f"{run['run']:<5}{run['total_factors']:>8}{c['match']:>7}"
              f"{c['valid_extra']:>7}{c['duplicate']:>5}{c['wrong']:>7}"
              f"{fmt(run['recall']):>8}{fmt(run['precision']):>11}")

    print()
    for metric in ("recall", "precision"):
        s = summary[metric]
        if s is None:
            print(f"{metric:<10} no successful runs")
        else:
            print(f"{metric:<10} mean {fmt(s['mean'])}  "
                  f"min {fmt(s['min'])}  max {fmt(s['max'])}")
    print(f"successful runs: {summary['successful_runs']}/{len(runs)}\n")


# ---------- Judge test ----------

def label_summary(item):
    """Label plus its target, e.g. 'duplicate of #5' or 'match K2+K4'."""
    if item["label"] == "duplicate":
        return f"duplicate of #{item['duplicate_of']}"
    if item["label"] == "match":
        return "match " + "+".join(f"K{k}" for k in item["key_ids"])
    return item["label"]


def run_judge_test(incident_text, key_factors, base_folder):
    """Judge run_1's factors plus planted bad factors, and check the judge catches them."""
    with open(os.path.join(base_folder, "run_1", "step4.json"), encoding="utf-8") as f:
        originals = json.load(f)["factors"]
    with open(os.path.join(base_folder, "scores.json"), encoding="utf-8") as f:
        baseline_run = next(r for r in json.load(f)["runs"] if r["run"] == 1)
    baseline = {item["id"]: item["label"] for item in baseline_run["labels"]}

    planted = [{"factor": p["factor"], "evidence": f"\"{p['evidence']}\""}
               for p in PLANTED_FACTORS]
    combined = originals + planted
    first_planted = len(originals) + 1

    runs = []
    for i in range(1, JUDGE_TEST_RUNS + 1):
        print(f"\n===== Judge test {i}/{JUDGE_TEST_RUNS} ({len(combined)} factors) =====")
        try:
            runs.append({"run": i, "labels": judge_factors(incident_text, key_factors, combined)})
        except Exception as error:  # record the failure and carry on with other runs
            print(f"  Run {i} failed: {error}")
            runs.append({"run": i, "error": f"{type(error).__name__}: {error}"})

    planted_results = []
    for n, p in enumerate(PLANTED_FACTORS, start=first_planted):
        per_run = []
        for run in runs:
            if "error" in run:
                per_run.append({"run": run["run"], "error": run["error"]})
                continue
            item = run["labels"][n - 1]
            entry = {"run": run["run"], "label": item["label"], "reason": item["reason"]}
            for field in ("key_ids", "duplicate_of"):
                if field in item:
                    entry[field] = item[field]
            per_run.append(entry)
        caught = sum(r.get("label") == p["expected"] for r in per_run)
        planted_results.append({
            "planted_id": p["id"], "judge_id": n, "factor": p["factor"],
            "evidence": p["evidence"], "expected": p["expected"],
            "runs": per_run, "caught": caught, "caught_text": f"{caught}/{len(runs)}",
        })

    drift = []
    for n, factor in enumerate(originals, start=1):
        labels = [None if "error" in run else run["labels"][n - 1]["label"] for run in runs]
        if any(label is not None and label != baseline[n] for label in labels):
            drift.append({"id": n, "factor": factor["factor"],
                          "baseline_label": baseline[n], "run_labels": labels})

    result = {
        "judge_model": JUDGE_MODEL,
        "source_factors": os.path.join(base_folder, "run_1", "step4.json"),
        "baseline": os.path.join(base_folder, "scores.json") + " (run 1)",
        "original_count": len(originals),
        "planted": planted_results,
        "original_label_changes": drift,
        "runs": runs,
    }
    agent.save_json(result, base_folder, "judge_test.json")

    print("\n========== JUDGE TEST ==========")
    for r in planted_results:
        labels = ["ERROR" if "error" in x else label_summary(x) for x in r["runs"]]
        print(f"{r['planted_id']} (#{r['judge_id']})  expected {r['expected']:<10} "
              f"caught {r['caught_text']}  runs: {' | '.join(labels)}")
    print(f"\nOriginal factors whose label changed from scores.json run 1: {len(drift)}")
    for d in drift:
        labels = ["ERROR" if label is None else label for label in d["run_labels"]]
        print(f"  #{d['id']}  baseline {d['baseline_label']}  runs: {' | '.join(labels)}")
    print()


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="Score the agent against an answer key")
    parser.add_argument("input_file", help="incident text file")
    parser.add_argument("answer_key", help="answer key file")
    parser.add_argument("--rescore", action="store_true",
                        help="skip the agent; re-judge the existing run_*/step4.json")
    parser.add_argument("--judge-test", action="store_true",
                        help="judge run_1's factors plus planted bad factors "
                             f"{JUDGE_TEST_RUNS} times and report which were caught")
    args = parser.parse_args()

    with open(args.input_file, encoding="utf-8") as f:
        incident_text = f.read()
    key_factors = load_key_factors(args.answer_key)

    name = os.path.splitext(os.path.basename(args.input_file))[0]
    base_folder = os.path.join("outputs", name)

    if args.judge_test:
        run_judge_test(incident_text, key_factors, base_folder)
        return

    runs = []
    for i in range(1, NUM_RUNS + 1):
        run_folder = os.path.join(base_folder, f"run_{i}")
        print(f"\n===== Run {i}/{NUM_RUNS} ({run_folder}) =====")
        try:
            if args.rescore:
                with open(os.path.join(run_folder, "step4.json"), encoding="utf-8") as f:
                    review = json.load(f)
            else:
                review = agent.run_agent(args.input_file, run_folder)["review"]

            agent_factors = review["factors"]
            print("Judging contributing factors...")
            labels = judge_factors(incident_text, key_factors, agent_factors)
            runs.append({"run": i, **score_run(labels, key_factors, agent_factors)})
        except Exception as error:  # record the failure and carry on with other runs
            print(f"  Run {i} failed: {error}")
            runs.append({"run": i, "error": f"{type(error).__name__}: {error}"})

    ok = [r for r in runs if "error" not in r]
    summary = {
        "successful_runs": len(ok),
        "recall": summarize([r["recall"] for r in ok]),
        "precision": summarize([r["precision"] for r in ok]),
    }
    scores = {
        "input_file": args.input_file,
        "answer_key": args.answer_key,
        "judge_model": JUDGE_MODEL,
        "rescored": args.rescore,
        "key_factors": key_factors,
        "runs": runs,
        "summary": summary,
    }
    os.makedirs(base_folder, exist_ok=True)
    agent.save_json(scores, base_folder, "scores.json")
    print_table(runs, summary)


if __name__ == "__main__":
    main()

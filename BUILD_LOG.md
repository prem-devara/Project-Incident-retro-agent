
# Build log: Incident Retrospective Agent

Notes for the README. Raw, dated, in build order.

## README highlights
- An LLM judge can be lenient in ways prompt instructions don't fix. I caught it accepting a backwards cause by hand, so the judge needs human spot-checks and its own test.

## Setup
- Built with Claude Code (VS Code) as the coding assistant; agent calls models through aisuite, so providers can be swapped.
- Kept agent input (`data/`) and answer keys (`eval/`) in separate folders so the coding assistant could not see the answers while building.
- Caught a leak risk: Claude Code auto-attaches whatever file is open in the editor. An answer-key file was attached to the first build prompt, so I rejected that session's code and rebuilt in a clean session.

## Steps 1–3 (first build)
- First run failed: aisuite reads only the first block of a model reply, and the model sent a "thinking" block first. Fixed by turning thinking off.
- Claude Code caught a Python bug in its own retry logic before running (the error variable is deleted after an `except` block).

## Run 1 results (GitLab 2017 database outage)
- 12 timeline events, 13 contributing factors, 13 action items.
- Recall: found all 5 published contributing factors that the input supports, plus a partial inference on one that was not in the input.
- Precision: 77%. Of 13 factors, 2 were wrong and 1 duplicated another.
  - One factor was backwards: it named a manual snapshot as a risk, but that snapshot is what enabled recovery and cut data loss from ~24h to ~6h.
  - One factor was speculation not supported by the input.
- 4 extra factors were valid and supported by the input; the published RCA covers some of them.

## Step 4: reflection
- Output cut off at the 2,000-token limit. Shortening fields didn't fix it. Raised the limit to 4,000 for Step 4 only.
- Lesson: 2,000 was an arbitrary starting value in my prompt, and the coding assistant treated it as a hard rule. AI tools follow stated constraints literally.
- Reflection with the same small model (Haiku) removed nothing: 12 factors before, 12 after, across two runs.
- The reviewer also logged changes it never made, even when told to log only real changes.
- Fix 1: reviewer must give a verdict per factor (keep / remove / merge with #N). Code applies the verdicts, so the change log always matches what happened.
- Fix 2: use a stronger model (Sonnet) for Step 4 only; Haiku stays on Steps 1–3.
- Different models in one pipeline needed different API settings (the "thinking" parameter). Swapping a model was not a one-line change.

## Step 4 on Sonnet with verdicts (run 3)
- Sonnet rejected the "thinking disabled" setting Haiku used; added a per-model setting.
- Factors 11 → 10 after review: 1 merge (two descriptions of the same backup problem), 0 removals, 5 rewordings.
- The evidence check worked through rewording: the reviewer dropped claims the report didn't support ("stress", "ambiguous host identity").
- Removed one action item that didn't address a contributing factor; made one vague action specific.
- Change log now matches reality: no reported changes that didn't happen, because code applies the verdicts.
- Caveat: Steps 1–3 regenerate on every run (11 factors this time vs 13 in run 1), so single runs aren't directly comparable. This is why the automated eval needs several runs per postmortem.
- Checked run 3's draft against run 1's two error types:
  - Mitigating action listed as a cause (the snapshot): did not appear in this draft, so the reviewer was not tested on it.
  - Speculation: appeared in a milder form. One factor claimed "stress" and "ambiguous host identity" without support; the reviewer removed those claims. A factor built on "asked for help on Slack" was merged into the backup factor rather than kept as a communication failure.

## Automated eval (LLM-as-judge)
- `evaluate.py` runs the full agent (Steps 1–4) 5 times, then a Sonnet judge (same settings as Step 4) labels each final factor: match / valid_extra / duplicate / wrong. Recall and precision are computed in code, not by the judge.
- Recall: 0.96 → 1.00 (mean of 5 runs) after fixing the judge's wording-based matching. It had marked factors as "valid_extra" when they described the same mechanism as a key line in different words (e.g. "no WAL archiving, manual resync" vs "replication broke").
- Precision: 1.00 on every run, according to the judge. My manual check found the judge accepted run 5 #11 (stale LVM snapshot "increasing data loss"), which is backwards: that snapshot limited data loss. My grading for run 5 is 10/11.
- Adding an explicit "wrong" rule to the judge prompt (anything that reduced harm or enabled recovery, named as a cause, is wrong) did not fix this; the judge still labelled #11 valid_extra.
- Lesson: the judge needs its own test. Plant known-wrong factors in a factor list and check that the judge catches them before trusting its precision numbers.
- The Step 4 reviewer catches a mitigating action listed as a cause only sometimes: in run 5 it removed #13 (manual snapshot named as a risk) but kept #11 (stale LVM snapshot "increasing data loss").

## Pending
- Test the judge with planted wrong factors.
- Automated eval (LLM-as-judge) across 10 postmortems.

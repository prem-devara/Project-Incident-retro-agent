
# Build log: Incident Retrospective Agent

Notes for the README. Raw, dated, in build order.

## README highlights
- An LLM judge can be lenient in ways prompt instructions don't fix. I caught it accepting a backwards cause by hand, so the judge needs human spot-checks and its own test.
- Planted errors were easy for the judge to catch (15/15); the agent's own subtle errors were not, so synthetic tests can overstate judge reliability.
- Two apparent agent weaknesses turned out to be scoring artifacts. I read the agent's actual factors before trusting a low score.
- On CrowdStrike, the agent named the kernel-mode driver as the reason a content bug crashed whole machines, a factor CrowdStrike's own findings left out.

## Results
Recall / precision, mean of 5 runs.

| Incident | Recall | Precision |
|---|---|---|
| GitLab 2017 | 1.00 | 1.00 |
| AWS S3 2017 | 0.96 | 1.00 |
| Google Cloud | 1.00 | 1.00 |
| AWS DynamoDB 2025 | 0.97 | 1.00 |
| CrowdStrike 2024 | 0.92 | 1.00 |

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
- Judge test (`evaluate.py --judge-test`): appended 5 planted bad factors (4 wrong, 1 duplicate) to run 1's 11 factors and judged the list 3 times. Caught 15/15 across 3 runs, and no drift on the originals (all 11 kept their scores.json labels).
- But the judge still accepted the agent's own subtler version of P1 (run 5 #11), so planted errors overstate judge reliability; real agent errors are subtler.

## AWS S3 2017 (second incident)
- Recall: 0.84, precision: 1.00 (mean of 5 runs).
- Runs 2–5 all missed the same factor: the tool allowed too much capacity to be removed at once, with no minimum-capacity check. Run 1 found it, so the input supports it.
- ~~Pattern: the agent tends to stop at the human error (the typo) instead of asking why the system allowed it.~~
- Correction: that "pattern" was a scoring artifact. The agent combined the typo and the missing safeguard in one factor, and the judge could credit only one answer-key line per factor.
- Fix: the judge now gives a list of key lines (`key_ids`) for each match, so a factor that combines two causes gets credit for both. After rescoring: AWS S3 recall 0.84 → 0.96, Google Cloud 0.83 → 1.00 (same cause: blank fields combined with missing validation in one factor), GitLab 1.00 (unchanged).
- The judge itself varies. On rescore, AWS S3 run 1 dropped 1.00 → 0.80 with no change in agent output: the judge matched the playbook factor to a different key line than last time.
- Kept the answer key unchanged rather than adjust it after seeing results.
- Positive: Step 4 correctly merged a mitigation (Twitter/banner status updates) into the dashboard factor instead of keeping it as a cause.

## AWS DynamoDB 2025 (fourth incident)
- Longest input so far (~3x GitLab). Step 1 was cut off at max_tokens=2000.
- 4 of 5 runs failed with a confusing JSONDecodeError ("Unterminated string"). The retry was wasted: it hit the same limit and was cut off again.
- Fix: Step 1 limit raised to 8,000. A reply cut off at max_tokens now raises a clear "cut off at max_tokens" error and is not retried.
- After the fix: 5/5 runs completed. Recall 0.97, precision 1.00 (mean of 5 runs).
- Spot-checked run 1: all matches single-line and fair.
- The one real miss: the Network Manager backlog. The agent noted that network state was slow to propagate but not why, skipping the middle link in a long failure chain.
- Lesson: token limits sized for short inputs don't scale. Truncation needs its own error, not a retry.

## CrowdStrike 2024 (fifth incident)
- Recall 0.92, precision 1.00 (mean of 5 runs). 5/5 runs completed.
- Runs 2 and 4 missed the same factor: new Template Instances shipped with only the Content Validator check, never tested on a sensor.
- Spot-checked run 2: a genuine miss, though that answer-key line is the most thinly supported by the input. Kept the key unchanged.
- The input is unusually explanatory (CrowdStrike's narrative is analysis-heavy), so scores here say less than DynamoDB's.
- Notable valid extras:
  - The kernel-mode driver turning a content bug into a full system crash, which CrowdStrike's own findings don't list.
  - Early successful deployments giving false assurance.

## Pending
- ~~Test the judge with planted wrong factors.~~ Done.
- ~~Automated eval across 5 incidents (GitLab, AWS S3, Google Cloud, AWS DynamoDB, CrowdStrike).~~ Done.
- Write README.
- Remove test.py, make repo public.

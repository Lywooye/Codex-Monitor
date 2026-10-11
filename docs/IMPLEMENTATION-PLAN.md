# Fork implementation scope

Authorized by the user's request to fork, create a local checkout, and improve per-turn/session accounting, compaction, tools and weekly reset accounting. Weekly attribution uses the user's selected proportional estimate, visibly labelled as such.

1. Read-only accounting: prefer response-ID-deduplicated native usage records, including compaction records; use advancing cumulative deltas only for legacy turns. Group by native turn ID; separate inherited fork history. Verify with known-answer fixtures and current local logs without publishing their contents.
2. Weekly estimates: build an account timeline from all discovered local logs; allocate each observed within-cycle percentage increment in proportion to recorded tokens. Separate reset deadlines and counter decreases; no allocation across unknown/reset boundaries. Display lifetime sum in percentage points, current-cycle estimate, per-cycle detail and coverage gaps. Never represent these as billing-accurate attribution.
3. UI: show turn and session totals, compaction totals and tool counts/names. Prefer stable turn identity; allow only unambiguous text matching, never positional fallback. Use synthetic UI fixtures, no injection into the user's app.
4. Preserve upstream provenance and license; document semantics, limits and verification. Leave installation/autostart disabled. The new clone has no CodeGraph index; direct source inspection is used, without creating one.

No raw prompts, responses, paths, credentials or real transcripts belong in the Git repository or test fixtures. Only synthetic tests and aggregate local validation results may be committed.

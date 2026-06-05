# Claude Code — project stats

A factual snapshot of the AI-assisted development of Sarmaya OS. Generated from
the local Claude Code session transcripts (`~/.claude/projects/...`) and `git`.

_Snapshot date: 2026-06-05._

## Model usage (token totals)

Across the project's Claude Code sessions:

| Metric | Value |
|--------|-------:|
| Sessions | 2 |
| Assistant responses | 1,765 |
| Tool calls (reads/edits/bash/etc.) | 875 |
| Transcript user entries (human turns + tool results) | 940 |
| Primary model | `claude-opus-4-8` |

### Tokens

| Bucket | Tokens |
|--------|-------:|
| Input (uncached) | 101,437 |
| Output | 1,314,116 |
| Cache creation | 6,489,501 |
| Cache read | 228,991,204 |
| **Total** | **236,896,258** (~237 M) |

Cache efficiency: ~96.6% of all input was served from the prompt cache
(228.99 M cache-read of ~237 M total), which is why a long multi-session build
stays economical — most context is re-read from cache, not re-billed at full
input rate. Cost depends on your plan/pricing tier, so no dollar figure is
asserted here.

> Note: in the transcript, tool results are recorded as "user" entries, so the
> 940 figure is not 940 human messages — human turns are a fraction of it.

## Codebase & git contribution

| Metric | Value |
|--------|-------:|
| Commits | 31 |
| Date range | 2025-12-05 → 2026-06-05 |
| Lines added | 36,438 |
| Lines removed | 1,677 |
| Python files | 112 |
| `app/` LOC | 8,915 |
| `tests/` LOC | 2,479 |
| Test functions | 150 |
| Tests passing (`pytest`, incl. parametrized) | 171 |

> Commits are authored under the repo owner's git identity (no AI co-author
> trailer, per project convention), so "Claude's share" can't be filtered from
> `git` by author — effectively the whole history was produced via Claude Code
> pair-development.

## What was built (high level)

- Multi-tenant FastAPI + Postgres AP-automation backend, layered
  api → service → repository → model, with Postgres RLS tenant isolation.
- MVP: invoice core, OCR + confidence/"why", exact + fuzzy duplicate detection
  with logged override, vendor master + review queue, governance gates, the
  Validated workflow step, audit trail, dashboard, email notifications.
- Governance-first platform layer (the four Build Book differentiators):
  configuration-first policies (+ tenant default provisioning), Live Audit Mode
  (with decision-time policy snapshots), Decision Inbox, and Restricted
  Autopilot.
- A review-and-fix pass: invoice create/update/delete/upload authorization,
  removed an unauthenticated upload placeholder, error-leak fixes + gating on AI
  endpoints, an N+1 fix, case-insensitive role checks, and live (non-stale)
  identity resolution from the DB.

## How to regenerate

```bash
# token/usage stats (parses the local session transcripts)
python - <<'PY'
import json, glob, os
d = os.path.expanduser('~/.claude/projects/C--python-sarmaya')
agg = dict(sessions=0,assistant=0,tools=0,inp=0,out=0,cc=0,cr=0)
for f in sorted(glob.glob(os.path.join(d,'*.jsonl'))):
    agg['sessions']+=1
    for line in open(f,encoding='utf-8'):
        line=line.strip()
        if not line: continue
        o=json.loads(line); m=o.get('message') or {}
        if o.get('type')=='assistant':
            agg['assistant']+=1; u=m.get('usage') or {}
            agg['inp']+=u.get('input_tokens',0); agg['out']+=u.get('output_tokens',0)
            agg['cc']+=u.get('cache_creation_input_tokens',0); agg['cr']+=u.get('cache_read_input_tokens',0)
            agg['tools']+=sum(1 for c in (m.get('content') or []) if isinstance(c,dict) and c.get('type')=='tool_use')
print(agg)
PY

# git stats
git rev-list --count HEAD
git log --pretty=tformat: --numstat | awk '{a+=$1;d+=$2} END{print "+"a" -"d}'
```

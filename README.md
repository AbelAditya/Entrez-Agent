# Entrez Agent

A LangGraph agent that answers biomedical questions by querying NCBI's [Entrez
E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25501/) — PubMed, PMC, Gene,
Protein, Nucleotide, MeSH and the rest of the ~38 Entrez databases — and reports
what it finds in prose with verifiable citations.

The agent does not answer from memory. Every claim about the literature comes
from a tool result in the same conversation.

```bash
python main.py
# Enter you query: How many PubMed articles have CRISPR in the title from 2023?
```

## Features

### Entrez tools
Six tools wrapping the E-utilities, in `tools.py`:

| Tool | Purpose |
|---|---|
| `e_search` | Find UIDs matching a query; returns counts, IDs and Entrez's query translation |
| `e_summary` | Lightweight records (title, authors, journal, dates, citation counts) for triage |
| `e_fetch` | Full records — abstracts, FASTA sequences, MEDLINE views |
| `e_link` | Related records: PubMed→PMC for full text, PubMed→PubMed for cited-by |
| `e_post` | Upload a UID list to the NCBI history server for sets over ~200 IDs |
| `e_info` | List databases, or a database's valid `[field]` tags and link names |

Each tool validates its arguments before calling NCBI (date formats, `retmax`
bounds, ID-list size, mutually required parameters), converts Biopython's parser
objects into plain dicts, and **returns errors as data** — `{"error": ...,
"tool": ...}` — instead of raising. The agent reads the error, corrects the call
and retries, rather than the graph crashing.

### Query translation validation
Entrez rewrites every query through Automatic Term Mapping, and it fails in
predictable ways: abbreviations fall back to free-text matching, multi-word
requests get swallowed as a single phrase, and terms map to the wrong sense.
Left unchecked, these produce confident answers over the wrong literature.

`e_search` returns `query_translation` verbatim, and the system prompt
(`prompts/agent.md`) requires the agent to check it against the request before
trusting the results: is every concept present as its own AND-ed group, did the
significant terms reach controlled vocabulary, did anything map to the wrong
sense, are the user's date and population constraints represented, and does the
result count fit the topic. Refinement is capped at two attempts, and the agent
must tell the user what it changed.

Measured examples of the failures this catches:

| Query | Entrez's interpretation | Result |
|---|---|---|
| `insulin resistance type 2 diabetes` | both concepts → `[MeSH Terms]` | 48,644 — correct |
| `ARBs kidney protection` | `"ARBs"[All Fields]`, no MeSH | 257 — misses the drug-class literature |
| `long covid brain fog` | one literal phrase, concepts never combined | 25 — far too few |

### Guardrails
`middleware.py` adds a `ContentCheckMiddleware` with two LLM checks:

- **Input** (`before_agent`): blocks out-of-scope requests, personal medical
  advice, prompt injection, and harmful protocols dressed in biomedical jargon —
  while allowing distressing subject matter (overdose, suicide, sexual health
  research), which is ordinary PubMed content.
- **Output** (`after_agent`): blocks individualised medical advice, actionable
  harm, leaked internals and instructions absorbed from retrieved abstracts. It
  distinguishes reporting from instructing: "a trial found 40 mg reduced
  relapse" passes; "take 40 mg daily" does not.

Both prompts live in `prompts/` and answer with a single word, so the verdict is
parsed deterministically. A blocked answer is replaced rather than appended, so
it never reaches the user. `PIIMiddleware` redacts email addresses.

### Rate limiting
`ratelimits.py` hands out one shared `InMemoryRateLimiter` per API key, so every
model in the process (agent, guardrail checks, eval judge) draws on the same
bucket — rate limits apply per key, not per model. Sized for the OpenRouter free
tier: 20 requests/minute, with a small burst allowance. Pacing requests is
cheaper than being rejected: a 429 costs a request and returns nothing.

### Trajectory evals
`evals/evals.py` runs the suite in `evals/evals.json` and grades **what the agent
did**, not just its final answer, using an `agentevals` LLM-as-judge against each
eval's reference trajectory.

```bash
python evals/evals.py --ids eval_01 eval_02   # a few evals, one run each
python evals/evals.py --budget 12             # spend at most 12 model requests
python evals/evals.py --score-only evals/runs.json  # re-grade saved runs, no agent calls
```

The suite currently has five evals: trusting a correct term mapping, overriding
an abbreviation that fell back to free text, decomposing a multi-concept request,
refining a query that returned too few results, and curating a large result set
through the history server.

Because a free-tier key allows 50 requests/day and **one eval run costs 4–6
requests** (one per agent model round, plus one per guardrail check), the runner
budgets in requests rather than runs: it stops cleanly when the remainder won't
cover another run, saves after every run, and names the evals still outstanding.
Runs accumulate in `runs.json` across days and can be graded together later.

## Caching (design)

Not yet implemented; this section records the design and why.

The binding constraint is **requests**, not tokens, so only caches that remove a
whole request help. Layers considered, against this system:

| Layer | What it caches | Requests saved per run | Verdict |
|---|---|---|---|
| **A. Guardrail verdicts** | `sha256(check prompt + text)` → `ALLOWED`/`BLOCKED` | 1–2 of ~5 | **Build.** Deterministic, no staleness: same text under the same policy always yields the same verdict |
| **B. Exact-match LLM cache** | every model call, keyed on the full serialised prompt | ~1 | **Skip.** Only the first agent round has a stable prompt; later rounds embed live tool results (counts and newest PMIDs change daily), so they always miss — and a cached first round makes the agent replay a stale tool call |
| **C. Strategy memory** | successful search strategies: `db`, final `term`, its translation, count | 2–3 (the whole refine loop) | **Build.** Replaces B's benefit without B's staleness: the agent still runs the search, so results are always fresh |
| **D. Whole-answer reuse** | query → final answer | all ~5 | **Later, if at all.** Literature answers age, and no trajectory is produced to grade |
| **E. Provider prompt caching** | the stable prefix (system prompt + tool schemas, ~5k tokens/round) | 0 | Worth enabling for cost and latency, but it reduces tokens, not request count |

### Why no embeddings
Strategy memory retrieves by **lexical token overlap (Dice coefficient), not
embedding similarity**, and gates every match on an exact match of the
distinctive tokens (anything containing a digit, capitalised in the original, or
absent from a small common-word list).

Embeddings are unsafe for this domain: they place biomedical abbreviations and
entity variants extremely close together despite referring to different things.
`BRCA1`/`BRCA2`, `ARBs`/`ACE inhibitors`, `type 1`/`type 2 diabetes` all sit at
high cosine similarity, so a similarity-only cache would answer one question
with another's search strategy — and the result looks entirely plausible,
citing real records for the wrong entity.

Lexical scoring is not immune (measured: `BRCA2` vs stored `BRCA1` scores 0.75,
`type 1` vs `type 2` scores 0.80), which is why the entity gate, not the score,
is the actual safety mechanism. Its failure mode is also the safer one: it misses
genuine paraphrases (`renal` vs `kidney`) rather than inventing false matches.
A missed match costs one refine loop; a false match misinforms.

Storage is Redis (TTL on caches, none on strategy memory, so `volatile-lru`
eviction can only ever drop disposable entries), with in-process fallback when
Redis is unreachable — a cache being down must never fail a query.

## Layout

```
agent.py         # create_agent: model, tools, middleware, system prompt
tools.py         # the six E-utilities tools
middleware.py    # input/output content checks
ratelimits.py    # shared per-key rate limiters
main.py          # interactive CLI
prompts/         # agent.md, input_check.md, output_check.md
evals/           # eval suite, runner, saved runs
```

## Setup

```bash
uv sync
```

`.env`:

```
ENTREZ_EMAIL=you@example.com     # required by NCBI
ENTREZ_API_KEY=...               # raises NCBI's limit to 10 requests/second
OPENROUTER_API_KEY=...
OPENROUTER_API_KEY_2=...         # agent, guardrails and eval judge currently use this
```

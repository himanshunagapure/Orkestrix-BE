# LinkedIn Recruiter Project — Issue History & Fixes

## What the user wanted

A 3-step workflow from `Process_configs.txt`:

1. **Screen 1** — Pick/upload a JD + `candidate_count` (1–30). New JDs go through Gemini extraction and save to `job_description`. Emit flat `jd_*` fields + `jd_id` + `candidate_count`.
2. **Tool** — Unipile LinkedIn search → enrich profiles → score → upsert `all_Candidates` → emit `candidates_batch_id`, counts.
3. **Screen 3 (Display Profiles)** — Show scored profile cards (photo, name, LinkedIn link, score badge/breakdown, skills, sort/filter).

The graph wiring (Screen1 → Tool → Screen3) was basically correct. Most failures were in **generated code quality**, **API contract mismatches**, and **credential/config mistakes**.

---

## Timeline of what broke (and why)

### Issue 1 — Screen 1 “succeeded” but emitted empty data

**Symptom (first `instances.txt`):** all `jd_*` empty, `candidate_count=0`, tool then no-op’d, Screen 3 blank.

**Root cause:** `process_job_search_config.py` was a **stub CRUD insert template**.

- Docstring described Gemini extract / existing-JD load / flat `jd_*` return.
- Body always did `action = "insert"` and returned `{recordId, collectionName, status}`.
- FE correctly called `__AIQOD_SCREEN__.complete({ jd_id: res.jd_id, ... })`, so missing keys became `undefined` → orchestrator defaults (`""` / `0` / `[]`).

**Why it occurred:** codegen/backend-generator often falls back to a generic DB template when the “real” function body isn’t fully implemented, while the FE still expects the declared response schema.

**Fix:** Rewrote `process_job_search_config.py` to:

- load existing JD by id, or
- call Gemini, persist, return flat `jd_id` / `candidate_count` / `jd_*`.

---

### Issue 2 — Pydantic response validation (500) after Screen 1 worked

**Symptom:** Gemini extraction succeeded, insert succeeded, then:

`Input should be a valid dictionary or instance of SkillItem` for list fields.

**Root cause:** Response model expects **objects**:

- `[{skill: "Python"}]`, `[{tool: "AWS"}]`, etc.

We returned **plain strings**: `["Python"]`.

**Why:** Workflow `itemSchema` / FE models use object items; generationDescription said “list of strings.” We followed the prompt text, not the runtime validator.

**Fix:** Wrap list outputs as `{skill}` / `{tool}` / `{responsibility}` / etc. Persist plain strings in Mongo; return objects to the API.

---

### Issue 3 — Screen 3 empty after Screen 1 fixed

**Symptom:** Screen 1 had real JD data; tool returned `fetched_candidates_count: 0` in ~1s; Screen 3 had batch id but no rows.

**Root causes (layered):**

1. **Wrong Unipile base URL/endpoints** in the tool
   - Used `https://api.unipile.com` + GET `/people/search`
   - Spec/docs: DSN like `api20.unipile.com:15002`, POST search, `/search/parameters`, profile with `linkedin_sections`

2. **Missing `https://` in credential**
   - User set `UNIPILE_API_URL=api20.unipile.com:15002`
   - Error: `No connection adapters were found for 'api20.unipile.com:15002/...'`
   - `requests` requires a scheme.

3. **Latent query mismatch** (would bite after fetch worked)
   - Tool stored `jd_id` on docs and set output `candidates_batch_id = jd_id`
   - Screen 3 queried only `candidates_batch_id` / `batch_id`, not `jd_id`

**Why:** Generated tool used approximate Unipile paths; credentials were pasted as host:port; Screen 3 fetch wasn’t aligned with how the tool keyed batches.

**Fixes:**

- Tool: correct Unipile endpoints, default DSN `https://api20.unipile.com:15002`, auto-prepend `https://`, persist `candidates_batch_id`, surface `error` when count is 0
- `fetch_candidates_batch`: also query by `jd_id`
- User: set `UNIPILE_API_URL=https://api20.unipile.com:15002`

After that, counts worked (e.g. 10 requested / 10 fetched).

---

### Issue 4 — Names showed as “Unknown”

**Symptom:** Counts correct; table showed `Unknown` / `—` for name/email; scores present.

**Root cause:**

- Unipile search/profile often omit usable `first_name`/`last_name` (or use `name` / `display_name`)
- Profile enrichment **replaced** the search object, dropping search identity fields
- Tool only mapped `first_name`/`last_name`/`email`
- Emails frequently unavailable on LinkedIn (`contact_info.emails` empty) — “—” is often expected
- FE: `candidate.candidate_name || 'Unknown'`

**Why:** Field-name mismatch + overwrite-on-enrich + LinkedIn privacy limits.

**Fixes:**

- Merge profile onto search result (don’t wipe names)
- Map `display_name` / `name` / `contact_info.emails` / `work_experience`
- Store `candidate_name`

---

### Issue 5 — Names showed as headlines

**Symptom:** Table “Candidate Name” column filled with LinkedIn headlines (skills pipes, `|`, `•`).

**Evidence from `all_Candidates` dump:**

```text
first_name: ""
last_name: ""
email: ""
linkedin_public_identifier: "tushar-patil-727125229"  ← present
headline: "Python Software Developer | FastAPI • Django ..."  ← present
```

**Root cause:** API still didn’t provide names. Our Screen 3 fallback order was:

`name → … → headline → linkedin_public_identifier`

So empty names fell through to **headline**.

**Why:** Defensive fallback chose the wrong “human readable” field; headlines look like text but aren’t names.

**Fixes:**

- Tool: if no name from API, derive/save from `linkedin_public_identifier` (e.g. `tushar-patil-727125229` → `Tushar Patil`)
- `fetch_candidates_batch`: prefer slug-derived name; **never** use headline as name

---

## Separate product/generation gap (not fully fixed)

**Screen 3 UI ≠ user Step 3 intent**

User asked for profile cards (photo, score badge colors, 5-part breakdown, matched skills, sort/filter, “Showing X of Y”).

Planner wrote a near-empty `generationDescription`:

> “receives inputs … to render a user interface.”

So the system generated a **batch summary + simple table**, not profile cards. Data-path fixes made the table work; the UX still doesn’t match the original Step 3 design. That needs a regen with a full Step 3 description.

Also still weaker vs spec in the tool: scoring details, throttle retries, some status/source string values, enrichment depth (`skills`/`experience` often empty in DB when sections don’t come back).

---

## Causal chain (short)

```text
Stub Screen 1 BE
  → empty jd_* / candidate_count=0
  → tool no-op
  → Screen 3 empty

Fixed Screen 1, but returned string lists
  → Pydantic SkillItem validation 500

Fixed list shapes + Unipile URL/endpoints + https://
  → fetch count works

Wrong name mapping / overwrite + headline fallback
  → Unknown, then headlines in the name column

Slug fallback
  → usable display names even when API omits first/last name
```

---

## What we changed (files)

| Area | File | Change |
|------|------|--------|
| Screen 1 BE | `ui_screens/screen_20260909102006_02d16c7d/backend-functions/process_job_search_config.py` | Real JD existing/new + Gemini + flat `jd_*`; object-shaped lists |
| Screen 3 BE | `ui_screens/screen_20260909103118_146ac4d2/backend-functions/fetch_candidates_batch.py` | Query `jd_id`; richer fields; name from slug, not headline |
| Tool | `tools/row_toc1mxpz8_0c09b8f7_sb_86dc311c_20260909_160118_e3dfbc5f.json` | Unipile API/DSN, `https://` normalize, merge profiles, name/email mapping, `candidates_batch_id`, slug-derived names, errors when 0 fetched |

---

## Bottom line

The workflow **intent and graph** were fine. Failures came from:

1. **Incomplete codegen** (stub BE, weak Screen 3 prompt)
2. **Schema mismatch** (strings vs SkillItem objects)
3. **Wrong/brittle Unipile integration** + missing URL scheme
4. **Identity field mapping** when LinkedIn doesn’t return names/emails

With the patches above, the pipeline can run end-to-end with real candidate rows and slug-based names. Remaining work for “as user expected” is mainly **regenerating Screen 3 as profile cards** and hardening enrichment/scoring against Unipile’s real response shapes.

---

## Related artifacts

- `Process_configs.txt` — original workflow intent / generation config
- `instances.txt` — execution traces showing empty → partial → working data flow
- `aiqod-dev_sb_5beaabd82ac6767c86dc311c.all_Candidates.json` — sample stored candidates (empty names, present `linkedin_public_identifier` + headline)

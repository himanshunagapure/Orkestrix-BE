# LinkedIn Intent Fidelity — Live LLM Trace

_Generated: 2026-09-15 (live Gemini run)_  
_Artifacts:_ `backend/scripts/eval_output/linkedin_intent_trace_20260915_074319/`  
_Runner:_ `backend/scripts/run_linkedin_intent_fidelity_live_trace.py` (+ `continue_linkedin_intent_trace_tool_backend.py`)

## Executive summary

Real LLM calls were run on the LinkedIn Recruitment sub-steps from `linkedin-recruiter/Process_configs.txt` through:

1. **scratch_graph_planner** Stages 1 → 1.2 → 2a → 4 (gap/code emission off)
2. **Gap UI → AIProcessor** for `candidate_profiles_screen` (enhance + IR + Step 5.5 `databaseLogic` enricher)
3. **Gap tool generator** for `linkedin_sourcing_tool` (intent-aware prompt + one codegen attempt)
4. **Backend path** analysis of IR dataSources (grounding only if `external` + no URL)

### How node descriptions transformed

| Node | Original step chars | Stage 1 skeleton | Stage 1.2 enriched | Final `intentDescription` |
|---|---:|---:|---:|---:|
| Screen 1 / `jd_input_screen` | 3189 | 1554 | **3642** | 3642 |
| Tool / `linkedin_sourcing_tool` | 7947 | 1603 | **4597** | 4597 |
| Screen 3 / `candidate_profiles_screen` | 1037 | 787 | **1840** | 1840 |

**Stage 1** compressed Tool 1 (7947→1603) but kept Unipile host + scoring outline.  
**Stage 1.2** restored density (tool 1603→4597) including `linkedin_sections`, Unipile URLs, scoring weights, `all_Candidates`.  
**Stage 4** froze the same text into both `generationDescription` and **`intentDescription`** (immutable).

### Gap UI → AIProcessor (`candidate_profiles_screen`)

- Gap prompt used **intentDescription** (1840 chars) + typed I/O contract — **not** a generic “receives inputs … UI” sentence.
- Enhancer output **8629 chars**; deterministic block **`ORIGINAL NODE SPECIFICATION`** present (**substring check True**).
- Screen-3 UX signals survived enhance: profile cards, score badge/breakdown, matched skills, sort/filter.
- IR + Step 5.5 produced `fetch_candidates` with **1376-char** `databaseLogic` (schema inspect, sort/filter, score mapping) — not a one-line CRUD blurb.

### Gap tool generator

- Prompt received full intent (Unipile / `linkedin_sections` / scoring / `all_Candidates` all True).
- One-shot LLM JSON **parsed** but returned **empty `function_code`** (0 chars). Likely context/output pressure from a very large flattened IO parameter list (many JD fields as tool params). **Intent preservation worked; codegen completeness for this oversized tool is a separate Issue-2/3-class problem.**

### Backend generator

- IR dataSources were **`dynamic`** (Mongo), not `external` → grounding correctly **not** invoked.
- Enriched `databaseLogic` already carried executable steps for `fetch_candidates` / `submit_review`.

### Issue 1 checklist (this run)

| Check | Result |
|---|---|
| Screen 3 intent retains cards / score / skills / sort | ✅ |
| Enhancer retains original (prepend) | ✅ |
| No I/O-only overwrite of intent at Stage 4 | ✅ |
| Tool intent still has Unipile + scoring | ✅ |
| Tool codegen emitted full function_code on first try | ❌ empty `function_code` |
| External API grounding needed for Screen 3 DS | N/A (Mongo dynamic) |

---


## 0. Original user requirements (authoritative)

### Screen 1 (`row_keetc3e3q`) — preferredKind=`ui_screen`

- chars: 3189
- fidelity signals: `{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`

<details><summary>Full original description</summary>

```
**Step 1: Job Description (JD) Input & Search Configuration**

The user either uploads a new Job Description or selects an existing one from the `job_description` collection. They also specify the **total number of matching candidates to fetch** (maximum: **30**).

- **If a new JD is uploaded:**
  Make an LLM call to Gemini with the following instruction:

  > *"You are an expert HR data extraction assistant. Analyze the provided Job Description text and extract all relevant information into a structured JSON object. Return only the JSON with the following fields — use `null` for any field that cannot be determined, and return arrays (even empty ones) for all array-type fields:*
  >
  > ```json
  > {
  >   "job_title": "Official title or designation of the position",
  >   "job_category": "Broad functional area or industry domain (e.g., Software Engineering, Accounting)",
  >   "company_name": "Name of the hiring company or organization",
  >   "department": "Specific internal team, division, or business unit",
  >   "location": "Primary physical location (city, state, country)",
  >   "work_mode": "Work arrangement type (e.g., Remote, Hybrid, Onsite)",
  >   "employment_type": "Engagement type (e.g., Full-Time, Part-Time, Contract, Internship)",
  >   "seniority_level": "Career tier (e.g., Entry-Level, Mid-Level, Senior, Lead, Manager, Executive)",
  >   "min_years_experience": "Minimum required years of relevant experience (number)",
  >   "max_years_experience": "Maximum target years of experience (number or null if open-ended)",
  >   "management_experience_required": "Boolean — whether people management or leadership experience is required",
  >   "required_skills": ["Array of non-negotiable technical, functional, or soft skills"],
  >   "preferred_skills": ["Array of nice-to-have or optional skills and qualifications"],
  >   "tools_and_technologies": ["Array of software, platforms, tools, or programming languages required"],
  >   "education_level": "Minimum academic qualification or degree needed",
  >   "certifications": ["Array of required or preferred professional certifications and licenses"],
  >   "key_responsibilities": ["Array of main duties, daily tasks, and role expectations"],
  >   "domain_expertise": ["Array of specific industry domains or technical practice areas"],
  >   "salary_min": "Minimum base salary (numeric value or null)",
  >   "salary_max": "Maximum base salary (numeric value or null)",
  >   "currency": "3-letter currency code (e.g., USD, INR, EUR) or null",
  >   "benefits": ["Array of perks, healthcare, allowances, and other work benefits"]
  > }
  > ```
  >
  > *Job Description Text: `{{jd_text}}`"*

  The extracted JSON is saved into the `job_description` collection along with the original JD text and a timestamp.

- **If an existing JD is selected:**
  Fetch the already-extracted structured JSON directly from the `job_description` collection using its ID.

- **In both cases**, the user also inputs:
  - `candidate_count` — Total number of matching LinkedIn profiles to fetch *(minimum: 1, maximum: 30)*

  Both the JD structured data and `candidate_count` are passed forward to the next step.

---
```
</details>

### Tool 1 (`row_toc1mxpz8`) — preferredKind=`tool`

- chars: 7947
- fidelity signals: `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}`

<details><summary>Full original description</summary>

```
## Step 2: Profile Fetching, Enrichment, Scoring & Storage *(Backend Tool)*

---

### 2a — Generate LinkedIn Search Query *(Gemini LLM)*

> *"You are a LinkedIn recruiting search expert. Given the structured Job Description JSON below, generate an optimized LinkedIn Classic people search query.*
>
> *Return a JSON object with exactly these fields:*
> - `keywords`: A single concise search string (max 200 characters) combining the most important job title, core skills, and domain context. Prioritize specificity over breadth. Do NOT use boolean operators.
> - `title`: The most likely LinkedIn profile headline or current job title that an ideal candidate for this role would have. Keep it short (max 60 characters).
> - `industry_hint`: The single most relevant LinkedIn industry category name for this role (e.g., "Information Technology and Services", "Financial Services").
>
> *Return only valid JSON. No explanation, no markdown.*
>
> *JD JSON: `{{jd_json}}`"*

---

### 2b — Resolve LinkedIn Filter IDs

```
GET https://api20.unipile.com:15002/v2/{account_id}/linkedin/search/parameters
    ?type=LOCATION&keyword={jd.location}
X-API-KEY: {key}
```
```
GET https://api20.unipile.com:15002/v2/{account_id}/linkedin/search/parameters
    ?type=INDUSTRY&keyword={gemini.industry_hint}
X-API-KEY: {key}
```
Take the first matching `id` from each. If no match, omit that filter.

---

### 2c — Paginated LinkedIn People Search

Loop until `candidate_count` profiles are collected (max 50, up to 5 pages of 10):

```
POST https://api20.unipile.com:15002/v2/{account_id}/linkedin/search/people
     ?limit=10&cursor={next_cursor if paginating}
X-API-KEY: {key}
```
```json
{
  "keywords": "{{gemini.keywords}}",
  "advanced_keywords": { "title": "{{gemini.title}}" },
  "location": ["{{resolved_location_id}}"],
  "industry": ["{{resolved_industry_id}}"],
  "network_distance": [2, 3]
}
```
Stop when collected count reaches `candidate_count` or `next_cursor` is absent. Trim to exact count.

---

### 2d — Fetch Detailed Profile for Each Candidate

For every collected profile, make one enrichment call using `linkedin_sections` to fetch all needed data:

```
GET https://api20.unipile.com:15002/api/v1/users/{public_identifier}
    ?account_id={account_id}
    &linkedin_sections=about,experience,education,skills,certifications
X-API-KEY: {key}
```

> **Why these sections?**
> - `about` → fills `summary` field
> - `experience` → fills `experience[]`, `total_years_of_experience`
> - `education` → fills `education[]`
> - `skills` → fills `skills[]` (LinkedIn endorsed skills — much richer than work_experience skills)
> - `certifications` → fills `certifications[]` (extra enrichment)
>
> Run all profile calls **asynchronously in parallel** with a small random delay (0.5–1s) between batches of 5 to avoid LinkedIn throttling. If a section comes back in `throttled_sections[]`, retry that profile once after a 2s delay.

---

### 2e — Schema Field Mapping to `all_Candidates` Collection

Map LinkedIn API response fields to the existing collection schema. Fields that LinkedIn does not expose publicly are stored as `null`:

| Schema Field | Source | LinkedIn Section | Notes |
|---|---|---|---|
| `file_name` | `null` | — | Not applicable for LinkedIn sourced |
| `resume_file_hash` | `null` | — | Not applicable |
| `resume_file_link` | `null` | — | Not applicable |
| `processed_at` | Current UTC timestamp | — | Set at time of save |
| `email` | `contact_info.emails[0]` | Base profile | `null` if empty |
| `phone` | `contact_info.phones[0]` | Base profile | `null` if empty |
| `first_name` | `first_name` | Base profile | |
| `last_name` | `last_name` | Base profile | |
| `location` | `location` | Base profile | e.g., "Agartala, Tripura, India" |
| `total_years_of_experience` | Computed (sum of all experience durations) | `experience` | Decimal, rounded to 1 place |
| `summary` | `about` text | `about` | LinkedIn About/bio section |
| `skills` | `skills[].name` array | `skills` | LinkedIn endorsed skills list |
| `education` | `education[]` mapped to `{degree, institution, year}` | `education` | |
| `experience` | `work_experience[]` mapped to `{title, company, start_date, end_date, description}` | `experience` | |
| `source` | `"linkedin"` | — | Fixed value |
| `why_interested_in_role` | `null` | — | Not available from LinkedIn |
| `actively_looking` | `"Yes"` if `is_open_to_work == true` else `"No"` | Base profile | |
| `current_notice_period` | `null` | — | Not available from LinkedIn |
| `earliest_join_date` | `null` | — | Not available from LinkedIn |
| `comfortable_working` | `null` | — | Not available from LinkedIn |
| `current_compensation` | `null` | — | Not public on LinkedIn |
| `compensation_expectations` | `null` | — | Not public on LinkedIn |
| `competing_offers` | `null` | — | Not applicable |
| `current_base` | `location` | Base profile | Same as location |
| `willing_to_relocate` | `null` | — | Not available from LinkedIn |
| `status` | `"linkedin_sourced"` | — | Fixed value |

**Additional LinkedIn-specific fields added to the document (extend schema):**

| New Field | Source | Description |
|---|---|---|
| `linkedin_profile_url` | `profile_url` | Full LinkedIn URL |
| `linkedin_public_identifier` | `public_identifier` | e.g., `"kanishsingha09"` |
| `linkedin_provider_id` | `provider_id` | LinkedIn's internal member ID |
| `headline` | `headline` | LinkedIn profile headline |
| `profile_picture_url` | `profile_picture_url` | Profile photo URL |
| `network_distance` | from search result | SECOND_DEGREE / THIRD_DEGREE |
| `certifications` | `certifications[]` | List of certifications from LinkedIn |
| `jd_id` | from Step 1 | Reference to the `job_description` document |
| `score` | Computed in 2f | Final score out of 100 |
| `score_breakdown` | Computed in 2f | `{skills_score, experience_score, education_score, open_to_work_score, contact_score}` |
| `matched_skills` | Computed in 2f | Array of JD skills found in candidate profile |

---

### 2f — Score Each Profile (Non-LLM, Rule-Based)

**① Skills — 30 points**

Build JD skill set:
```
jd_skills = jd.required_skills + jd.tools_and_technologies + jd.preferred_skills
```
Build candidate skill text (lowercase):
```
candidate_text = skills[].name + work_experience[].skills + work_experience[].description + headline
```
For each skill in `jd_skills`, check substring match in `candidate_text`. Track which skills matched in `matched_skills[]`.
```
skills_score = (matched_count / total jd_skills count) × 30   [cap at 30]
```

**② Experience — 30 points**

For each `work_experience` entry, compute duration:
- `start` → parse as date
- `end` → parse as date, or use today if `null`
- Sum all durations in decimal years

```
if jd.min_years_experience is null → experience_score = 30
elif total_years >= jd.min_years_experience → experience_score = 30
else → experience_score = (total_years / jd.min_years_experience) × 30   [min 0]
```

**③ Education — 20 points**

Degree tier mapping:
```
PhD / Doctorate        → 5
Master's / MBA / MS    → 4
Bachelor's / BE / BS   → 3
Diploma / Associate    → 2
High School / 12th     → 1
Not found              → 0
```
Find highest tier from `education[]`. Map `jd.education_level` to a tier.
```
gap = jd_tier - candidate_tier
gap <= 0  → 20
gap == 1  → 14
gap == 2  → 7
gap >= 3  → 0
jd.education_level is null → 20
```

**④ Open to Work — 10 points**
```
is_open_to_work == True → 10,  else → 0
```

**⑤ Contact Info — 10 points**
```
email available → 5 pts
phone available → 5 pts
```

**Total: `score = skills + experience + education + open_to_work + contact`**

---

### 2g — Deduplicate & Save to `all_Candidates`

Before inserting, check if `linkedin_public_identifier` already exists in `all_Candidates`. If it does, **update** the existing document (upsert). If not, **insert** as a new document.

Sort the full list by `score` descending and return to Step 3.
---
```
</details>

### Screen 2 (`row_o5h4okahg`) — preferredKind=`ui_screen`

- chars: 829
- fidelity signals: `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": true, "all_candidates_collection": false, "candidate_count_max_30": false}`

<details><summary>Full original description</summary>

```
## Step 3: Display Profiles in UI

Render the sorted profile list returned by Step 2 as profile cards in the UI.

**Each profile card displays:**
- Profile photo + Full name + LinkedIn profile link
- Headline & Location
- **Score badge:** `total_score / 100` with a color indicator (green ≥ 70, orange 40–69, red < 40)
- **Score breakdown bar** showing the 5 weighted components visually
- Total years of experience
- Open to Work badge (if `is_open_to_work = true`)
- Matched skills highlighted (chips/tags)
- Contact info: email and phone (if available)
- Top 2–3 most recent work experiences (company + position + duration)

**List-level UI:**
- Show total profiles found (e.g. `Showing 32 of 50 requested`)
- Allow sorting toggle (by score, by experience, by network distance)
- Allow filtering by minimum score threshold
---
```
</details>

## 1. Scratch graph planner — description evolution

- LLM calls: 5
- Checkpoints: ['stage1', 'stage1_2', 'stage1_7', 'stage2a', 'stage4c']

### `stage1`

- **`jd_input_screen`** (ui_screen) chars=1554 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`
- **`linkedin_sourcing_tool`** (tool) chars=1603 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`
- **`candidate_profiles_screen`** (ui_screen) chars=787 fidelity=`{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": false}`

#### `jd_input_screen` description

```
Create a UI screen with a file upload/text area for 'New Job Description' (label='Upload/Paste JD') OR a dropdown to select an existing JD (label='Select Existing JD'). Add a number input for 'Candidate Count' (label='Number of Candidates', min=1, max=30). Submit button labelled 'Configure Search'. On submit, if a new JD is provided, call Gemini LLM to extract: job_title, job_category, company_name, department, location, work_mode, employment_type, seniority_level, min_years_experience (number), max_years_experience (number), management_experience_required (boolean), required_skills (list), preferred_skills (list), tools_and_technologies (list), education_level, certifications (list), key_responsibilities (list), domain_expertise (list), salary_min (number), salary_max (number), currency, benefits (list). Save to Collection 'job_description' schema: {job_title:string, job_category:string, company_name:string, department:string, location:string, work_mode:string, employment_type:string, seniority_level:string, min_years_experience:number, max_years_experience:number, management_experience_required:boolean, required_skills:list, preferred_skills:list, tools_and_technologies:list, education_level:string, certifications:list, key_responsibilities:list, domain_expertise:list, salary_min:number, salary_max:number, currency:string, benefits:list, jd_text:string}. If existing JD is selected, fetch these fields from 'job_description' by ID. Output: jd_id (string), candidate_count (number), and all the extracted JD scalar and list fields.
```

#### `linkedin_sourcing_tool` description

```
Tool receives jd_id, candidate_count, and all extracted JD fields. 1) Call Gemini LLM to generate a LinkedIn search query returning keywords, title, and industry_hint. 2) Call Unipile API (api20.unipile.com:15002) to resolve location and industry_hint to LinkedIn filter IDs. 3) Call Unipile API paginated people search using resolved IDs and keywords until candidate_count is reached (max 30). 4) For each profile, call Unipile API in parallel (with 0.5-1s delay) to fetch sections: about, experience, education, skills, certifications. Retry throttled sections once after 2s. 5) Map fields and compute scores: Skills (30 pts based on matched_skills ratio), Experience (30 pts based on total_years_of_experience vs min_years_experience), Education (20 pts based on degree tier match), Open to Work (10 pts if actively_looking=='Yes'), Contact (5 pts email, 5 pts phone). 6) Upsert each profile into Collection 'all_Candidates' schema: {email:string, phone:string, first_name:string, last_name:string, location:string, total_years_of_experience:number, summary:string, skills:list, education:list, experience:list, source:string, actively_looking:string, current_base:string, status:string, linkedin_profile_url:string, linkedin_public_identifier:string, linkedin_provider_id:string, headline:string, profile_picture_url:string, network_distance:string, certifications:list, jd_id:string, score:number, score_breakdown:list, matched_skills:list} matching on linkedin_public_identifier. Set source='linkedin', status='linkedin_sourced'. Output: batch_id (string, set to jd_id) and fetched_count (number).
```

#### `candidate_profiles_screen` description

```
UI screen to display fetched candidates. Backend handler reads batch_id and fetches profiles from Collection 'all_Candidates' where jd_id matches the batch_id, sorted by score descending. UI displays a list-level header showing fetched_count (e.g., 'Showing X profiles'), a sort toggle (score, experience, network distance), and a minimum score filter. Renders a profile card for each candidate showing: profile_picture_url, first_name, last_name, linkedin_profile_url, headline, location, score (with color badge: green>=70, orange 40-69, red<40), score_breakdown (visual bar), total_years_of_experience, actively_looking (badge if 'Yes'), matched_skills (chips), email, phone, and top 2-3 experience entries. Submit button labelled 'Finish Review'. Outputs: review_completed (boolean).
```

### `stage1_2`

- **`jd_input_screen`** (ui_screen) chars=3642 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`
- **`linkedin_sourcing_tool`** (tool) chars=4597 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}`
- **`candidate_profiles_screen`** (ui_screen) chars=1840 fidelity=`{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`

#### `jd_input_screen` description

```
UI Screen: Job Description Input & Search Config. 

LAYOUT & CONTROLS:
1. JD Source Toggle: Radio group or tabs to select between 'Upload/Paste JD' (New) and 'Select Existing JD' (Existing).
2. New JD Section: 
   - Text area for pasting JD text (placeholder: 'Paste full job description here...').
   - Optional file upload (PDF/DOCX) that extracts text client-side or via standard backend parser to populate the text area.
3. Existing JD Section:
   - Dropdown populated by querying the 'job_description' collection (display 'job_title' + 'company_name', value '_id').
4. Search Configuration:
   - Number input labeled 'Number of Candidates' (min=1, max=30, required, default=10). Binds to 'candidate_count'.
5. Submit Action: Button labeled 'Configure Search'.

BACKEND HANDLER:
- If 'Select Existing JD': Fetch document from 'job_description' collection by ID. Proceed to output.
- If 'Upload/Paste JD':
  1. Call Gemini LLM with exact prompt:
     "You are an expert HR data extraction assistant. Analyze the provided Job Description text and extract all relevant information into a structured JSON object. Return only the JSON with the following fields — use null for any field that cannot be determined, and return arrays (even empty ones) for all array-type fields:
     {
       "job_title": "Official title or designation of the position",
       "job_category": "Broad functional area or industry domain (e.g., Software Engineering, Accounting)",
       "company_name": "Name of the hiring company or organization",
       "department": "Specific internal team, division, or business unit",
       "location": "Primary physical location (city, state, country)",
       "work_mode": "Work arrangement type (e.g., Remote, Hybrid, Onsite)",
       "employment_type": "Engagement type (e.g., Full-Time, Part-Time, Contract, Internship)",
       "seniority_level": "Career tier (e.g., Entry-Level, Mid-Level, Senior, Lead, Manager, Executive)",
       "min_years_experience": "Minimum required years of relevant experience (number)",
       "max_years_experience": "Maximum target years of experience (number or null if open-ended)",
       "management_experience_required": "Boolean — whether people management or leadership experience is required",
       "required_skills": ["Array of non-negotiable technical, functional, or soft skills"],
       "preferred_skills": ["Array of nice-to-have or optional skills and qualifications"],
       "tools_and_technologies": ["Array of software, platforms, tools, or programming languages required"],
       "education_level": "Minimum academic qualification or degree needed",
       "certifications": ["Array of required or preferred professional certifications and licenses"],
       "key_responsibilities": ["Array of main duties, daily tasks, and role expectations"],
       "domain_expertise": ["Array of specific industry domains or technical practice areas"],
       "salary_min": "Minimum base salary (numeric value or null)",
       "salary_max": "Maximum base salary (numeric value or null)",
       "currency": "3-letter currency code (e.g., USD, INR, EUR) or null",
       "benefits": ["Array of perks, healthcare, allowances, and other work benefits"]
     }
     Job Description Text: {{jd_text}}"
  2. Parse LLM response. If invalid JSON, retry once, then surface error to user.
  3. Insert into 'job_description' collection: { ...extracted_json, jd_text: "<original_text>", created_at: <UTC_timestamp> }.

OUTPUTS:
- jd_id (string: MongoDB ObjectId)
- candidate_count (number)
- All 22 extracted JD fields (job_title, required_skills, etc.) passed forward in workflow state.
```

#### `linkedin_sourcing_tool` description

```
Backend Tool: LinkedIn Sourcing & Scoring Tool. 

INPUTS: jd_id, candidate_count, and structured JD JSON from jd_input_screen.

STEP 2a: GENERATE SEARCH QUERY
Call Gemini LLM with exact prompt:
"You are a LinkedIn recruiting search expert. Given the structured Job Description JSON below, generate an optimized LinkedIn Classic people search query.
Return a JSON object with exactly these fields:
- keywords: A single concise search string (max 200 characters) combining the most important job title, core skills, and domain context. Prioritize specificity over breadth. Do NOT use boolean operators.
- title: The most likely LinkedIn profile headline or current job title that an ideal candidate for this role would have. Keep it short (max 60 characters).
- industry_hint: The single most relevant LinkedIn industry category name for this role (e.g., 'Information Technology and Services', 'Financial Services').
Return only valid JSON. No explanation, no markdown.
JD JSON: {{jd_json}}"

STEP 2b: RESOLVE FILTER IDS
Call Unipile API (api20.unipile.com:15002) with header 'X-API-KEY: {key}':
1. GET /v2/{account_id}/linkedin/search/parameters?type=LOCATION&keyword={jd.location}
2. GET /v2/{account_id}/linkedin/search/parameters?type=INDUSTRY&keyword={gemini.industry_hint}
Extract the first matching 'id' from each response. If no match, omit that filter in Step 2c.

STEP 2c: PAGINATED SEARCH
Loop POST /v2/{account_id}/linkedin/search/people?limit=10&cursor={next_cursor}
Body: {"keywords": "{{gemini.keywords}}", "advanced_keywords": { "title": "{{gemini.title}}" }, "location": ["{{resolved_location_id}}"], "industry": ["{{resolved_industry_id}}"], "network_distance": [2, 3]}
Stop when collected profiles >= candidate_count OR next_cursor is absent OR 5 pages (50 profiles) reached. Trim array to exact candidate_count.

STEP 2d: FETCH DETAILED PROFILES
For each profile, call GET /api/v1/users/{public_identifier}?account_id={account_id}&linkedin_sections=about,experience,education,skills,certifications
Execution: Async parallel, batch size 5, random delay 0.5-1s between batches. If response contains 'throttled_sections[]', retry that specific profile once after a 2s delay.

STEP 2e: SCHEMA MAPPING
Map to 'all_Candidates' collection schema. Unexposed fields must be explicit nulls.
- file_name, resume_file_hash, resume_file_link, why_interested_in_role, current_notice_period, earliest_join_date, comfortable_working, current_compensation, compensation_expectations, competing_offers, willing_to_relocate = null
- processed_at = UTC now
- email = contact_info.emails[0] (or null)
- phone = contact_info.phones[0] (or null)
- first_name = first_name, last_name = last_name, location = location, current_base = location
- total_years_of_experience = sum of experience durations (decimal, 1 place)
- summary = about
- skills = skills[].name
- education = education[] mapped to {degree, institution, year}
- experience = work_experience[] mapped to {title, company, start_date, end_date, description}
- source = "linkedin", status = "linkedin_sourced"
- actively_looking = "Yes" if is_open_to_work == true else "No"
- linkedin_profile_url = profile_url, linkedin_public_identifier = public_identifier, linkedin_provider_id = provider_id, headline = headline, profile_picture_url = profile_picture_url, network_distance = network_distance, certifications = certifications[], jd_id = jd_id.

STEP 2f: SCORING (Rule-Based)
1. Skills (30 pts): jd_skills = jd.required_skills + jd.tools_and_technologies + jd.preferred_skills. candidate_text = skills[].name + work_experience[].skills + work_experience[].description + headline (all lowercase). Check substring matches. skills_score = (matched_count / total jd_skills count) * 30 (cap 30). Save to matched_skills[].
2. Experience (30 pts): Sum durations. If jd.min_years_experience is null OR total_years >= jd.min_years_experience -> 30. Else -> (total_years / jd.min_years_experience) * 30.
3. Education (20 pts): Tiers: PhD=5, Master's=4, Bachelor's=3, Diploma=2, High School=1, None=0. gap = jd_tier - candidate_tier. gap<=0 -> 20, gap==1 -> 14, gap==2 -> 7, gap>=3 -> 0. If jd.education_level is null -> 20.
4. Open to Work (10 pts): is_open_to_work == true -> 10, else 0.
5. Contact (10 pts): email present -> 5, phone present -> 5.
Total score = sum of above. Save 'score' and 'score_breakdown' object.

STEP 2g: DEDUPLICATE & SAVE
Upsert each profile into 'all_Candidates' collection matching on 'linkedin_public_identifier'. Sort final array by score descending.

OUTPUTS: batch_id (string, equals jd_id), fetched_count (number).
```

#### `candidate_profiles_screen` description

```
UI Screen: Candidate Profiles UI. 

DATA FETCHING:
Backend handler queries the 'all_Candidates' collection where `jd_id` == `batch_id` (passed from previous node). Sort results by `score` descending by default.

LIST-LEVEL UI:
1. Header: Display "Showing {fetched_count} of {candidate_count} requested" (e.g., 'Showing 30 of 30 requested').
2. Controls Bar:
   - Sort Toggle: Dropdown to sort by 'Score' (default), 'Experience' (total_years_of_experience), or 'Network Distance'.
   - Filter: Number input or slider for 'Minimum Score Threshold' (0-100) that dynamically filters the rendered list.

PROFILE CARD LAYOUT (Rendered for each candidate):
1. Header Area:
   - `profile_picture_url` (circular avatar, fallback to initials if null).
   - Full Name (`first_name` + `last_name`).
   - LinkedIn Icon linking to `linkedin_profile_url` (target=_blank).
   - `headline` (subtext) and `location` (subtext).
2. Scoring Area:
   - Score Badge: Display `score / 100`. Color logic: Green if >= 70, Orange if 40-69, Red if < 40.
   - Score Breakdown Bar: A segmented visual progress bar showing the 5 components from `score_breakdown` (Skills, Experience, Education, Open to Work, Contact).
3. Key Highlights:
   - Total years of experience: Display `total_years_of_experience` + " Years".
   - Open to Work Badge: Render a prominent badge only if `actively_looking` == "Yes".
   - Matched Skills: Render `matched_skills` array as highlighted chips/tags.
4. Contact Info:
   - Display `email` and `phone` with copy-to-clipboard icons. If null, display "Not provided".
5. Experience Summary:
   - List the top 2-3 most recent entries from the `experience` array (format: Company + Position + Duration).

ACTIONS:
- Primary Submit Button: Labeled 'Finish Review'.
- On submit, transition to end node.

OUTPUTS:
- review_completed (boolean: true)
```

### `stage1_7`

- **`jd_input_screen`** (ui_screen) chars=3642 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`
- **`linkedin_sourcing_tool`** (tool) chars=4597 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}`
- **`candidate_profiles_screen`** (ui_screen) chars=1840 fidelity=`{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`

#### `jd_input_screen` description

```
UI Screen: Job Description Input & Search Config. 

LAYOUT & CONTROLS:
1. JD Source Toggle: Radio group or tabs to select between 'Upload/Paste JD' (New) and 'Select Existing JD' (Existing).
2. New JD Section: 
   - Text area for pasting JD text (placeholder: 'Paste full job description here...').
   - Optional file upload (PDF/DOCX) that extracts text client-side or via standard backend parser to populate the text area.
3. Existing JD Section:
   - Dropdown populated by querying the 'job_description' collection (display 'job_title' + 'company_name', value '_id').
4. Search Configuration:
   - Number input labeled 'Number of Candidates' (min=1, max=30, required, default=10). Binds to 'candidate_count'.
5. Submit Action: Button labeled 'Configure Search'.

BACKEND HANDLER:
- If 'Select Existing JD': Fetch document from 'job_description' collection by ID. Proceed to output.
- If 'Upload/Paste JD':
  1. Call Gemini LLM with exact prompt:
     "You are an expert HR data extraction assistant. Analyze the provided Job Description text and extract all relevant information into a structured JSON object. Return only the JSON with the following fields — use null for any field that cannot be determined, and return arrays (even empty ones) for all array-type fields:
     {
       "job_title": "Official title or designation of the position",
       "job_category": "Broad functional area or industry domain (e.g., Software Engineering, Accounting)",
       "company_name": "Name of the hiring company or organization",
       "department": "Specific internal team, division, or business unit",
       "location": "Primary physical location (city, state, country)",
       "work_mode": "Work arrangement type (e.g., Remote, Hybrid, Onsite)",
       "employment_type": "Engagement type (e.g., Full-Time, Part-Time, Contract, Internship)",
       "seniority_level": "Career tier (e.g., Entry-Level, Mid-Level, Senior, Lead, Manager, Executive)",
       "min_years_experience": "Minimum required years of relevant experience (number)",
       "max_years_experience": "Maximum target years of experience (number or null if open-ended)",
       "management_experience_required": "Boolean — whether people management or leadership experience is required",
       "required_skills": ["Array of non-negotiable technical, functional, or soft skills"],
       "preferred_skills": ["Array of nice-to-have or optional skills and qualifications"],
       "tools_and_technologies": ["Array of software, platforms, tools, or programming languages required"],
       "education_level": "Minimum academic qualification or degree needed",
       "certifications": ["Array of required or preferred professional certifications and licenses"],
       "key_responsibilities": ["Array of main duties, daily tasks, and role expectations"],
       "domain_expertise": ["Array of specific industry domains or technical practice areas"],
       "salary_min": "Minimum base salary (numeric value or null)",
       "salary_max": "Maximum base salary (numeric value or null)",
       "currency": "3-letter currency code (e.g., USD, INR, EUR) or null",
       "benefits": ["Array of perks, healthcare, allowances, and other work benefits"]
     }
     Job Description Text: {{jd_text}}"
  2. Parse LLM response. If invalid JSON, retry once, then surface error to user.
  3. Insert into 'job_description' collection: { ...extracted_json, jd_text: "<original_text>", created_at: <UTC_timestamp> }.

OUTPUTS:
- jd_id (string: MongoDB ObjectId)
- candidate_count (number)
- All 22 extracted JD fields (job_title, required_skills, etc.) passed forward in workflow state.
```

#### `linkedin_sourcing_tool` description

```
Backend Tool: LinkedIn Sourcing & Scoring Tool. 

INPUTS: jd_id, candidate_count, and structured JD JSON from jd_input_screen.

STEP 2a: GENERATE SEARCH QUERY
Call Gemini LLM with exact prompt:
"You are a LinkedIn recruiting search expert. Given the structured Job Description JSON below, generate an optimized LinkedIn Classic people search query.
Return a JSON object with exactly these fields:
- keywords: A single concise search string (max 200 characters) combining the most important job title, core skills, and domain context. Prioritize specificity over breadth. Do NOT use boolean operators.
- title: The most likely LinkedIn profile headline or current job title that an ideal candidate for this role would have. Keep it short (max 60 characters).
- industry_hint: The single most relevant LinkedIn industry category name for this role (e.g., 'Information Technology and Services', 'Financial Services').
Return only valid JSON. No explanation, no markdown.
JD JSON: {{jd_json}}"

STEP 2b: RESOLVE FILTER IDS
Call Unipile API (api20.unipile.com:15002) with header 'X-API-KEY: {key}':
1. GET /v2/{account_id}/linkedin/search/parameters?type=LOCATION&keyword={jd.location}
2. GET /v2/{account_id}/linkedin/search/parameters?type=INDUSTRY&keyword={gemini.industry_hint}
Extract the first matching 'id' from each response. If no match, omit that filter in Step 2c.

STEP 2c: PAGINATED SEARCH
Loop POST /v2/{account_id}/linkedin/search/people?limit=10&cursor={next_cursor}
Body: {"keywords": "{{gemini.keywords}}", "advanced_keywords": { "title": "{{gemini.title}}" }, "location": ["{{resolved_location_id}}"], "industry": ["{{resolved_industry_id}}"], "network_distance": [2, 3]}
Stop when collected profiles >= candidate_count OR next_cursor is absent OR 5 pages (50 profiles) reached. Trim array to exact candidate_count.

STEP 2d: FETCH DETAILED PROFILES
For each profile, call GET /api/v1/users/{public_identifier}?account_id={account_id}&linkedin_sections=about,experience,education,skills,certifications
Execution: Async parallel, batch size 5, random delay 0.5-1s between batches. If response contains 'throttled_sections[]', retry that specific profile once after a 2s delay.

STEP 2e: SCHEMA MAPPING
Map to 'all_Candidates' collection schema. Unexposed fields must be explicit nulls.
- file_name, resume_file_hash, resume_file_link, why_interested_in_role, current_notice_period, earliest_join_date, comfortable_working, current_compensation, compensation_expectations, competing_offers, willing_to_relocate = null
- processed_at = UTC now
- email = contact_info.emails[0] (or null)
- phone = contact_info.phones[0] (or null)
- first_name = first_name, last_name = last_name, location = location, current_base = location
- total_years_of_experience = sum of experience durations (decimal, 1 place)
- summary = about
- skills = skills[].name
- education = education[] mapped to {degree, institution, year}
- experience = work_experience[] mapped to {title, company, start_date, end_date, description}
- source = "linkedin", status = "linkedin_sourced"
- actively_looking = "Yes" if is_open_to_work == true else "No"
- linkedin_profile_url = profile_url, linkedin_public_identifier = public_identifier, linkedin_provider_id = provider_id, headline = headline, profile_picture_url = profile_picture_url, network_distance = network_distance, certifications = certifications[], jd_id = jd_id.

STEP 2f: SCORING (Rule-Based)
1. Skills (30 pts): jd_skills = jd.required_skills + jd.tools_and_technologies + jd.preferred_skills. candidate_text = skills[].name + work_experience[].skills + work_experience[].description + headline (all lowercase). Check substring matches. skills_score = (matched_count / total jd_skills count) * 30 (cap 30). Save to matched_skills[].
2. Experience (30 pts): Sum durations. If jd.min_years_experience is null OR total_years >= jd.min_years_experience -> 30. Else -> (total_years / jd.min_years_experience) * 30.
3. Education (20 pts): Tiers: PhD=5, Master's=4, Bachelor's=3, Diploma=2, High School=1, None=0. gap = jd_tier - candidate_tier. gap<=0 -> 20, gap==1 -> 14, gap==2 -> 7, gap>=3 -> 0. If jd.education_level is null -> 20.
4. Open to Work (10 pts): is_open_to_work == true -> 10, else 0.
5. Contact (10 pts): email present -> 5, phone present -> 5.
Total score = sum of above. Save 'score' and 'score_breakdown' object.

STEP 2g: DEDUPLICATE & SAVE
Upsert each profile into 'all_Candidates' collection matching on 'linkedin_public_identifier'. Sort final array by score descending.

OUTPUTS: batch_id (string, equals jd_id), fetched_count (number).
```

#### `candidate_profiles_screen` description

```
UI Screen: Candidate Profiles UI. 

DATA FETCHING:
Backend handler queries the 'all_Candidates' collection where `jd_id` == `batch_id` (passed from previous node). Sort results by `score` descending by default.

LIST-LEVEL UI:
1. Header: Display "Showing {fetched_count} of {candidate_count} requested" (e.g., 'Showing 30 of 30 requested').
2. Controls Bar:
   - Sort Toggle: Dropdown to sort by 'Score' (default), 'Experience' (total_years_of_experience), or 'Network Distance'.
   - Filter: Number input or slider for 'Minimum Score Threshold' (0-100) that dynamically filters the rendered list.

PROFILE CARD LAYOUT (Rendered for each candidate):
1. Header Area:
   - `profile_picture_url` (circular avatar, fallback to initials if null).
   - Full Name (`first_name` + `last_name`).
   - LinkedIn Icon linking to `linkedin_profile_url` (target=_blank).
   - `headline` (subtext) and `location` (subtext).
2. Scoring Area:
   - Score Badge: Display `score / 100`. Color logic: Green if >= 70, Orange if 40-69, Red if < 40.
   - Score Breakdown Bar: A segmented visual progress bar showing the 5 components from `score_breakdown` (Skills, Experience, Education, Open to Work, Contact).
3. Key Highlights:
   - Total years of experience: Display `total_years_of_experience` + " Years".
   - Open to Work Badge: Render a prominent badge only if `actively_looking` == "Yes".
   - Matched Skills: Render `matched_skills` array as highlighted chips/tags.
4. Contact Info:
   - Display `email` and `phone` with copy-to-clipboard icons. If null, display "Not provided".
5. Experience Summary:
   - List the top 2-3 most recent entries from the `experience` array (format: Company + Position + Duration).

ACTIONS:
- Primary Submit Button: Labeled 'Finish Review'.
- On submit, transition to end node.

OUTPUTS:
- review_completed (boolean: true)
```

### `stage2a`

- **`jd_input_screen`** (ui_screen) chars=3642 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`
- **`linkedin_sourcing_tool`** (tool) chars=4597 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}`
- **`candidate_profiles_screen`** (ui_screen) chars=1840 fidelity=`{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`

#### `jd_input_screen` description

```
UI Screen: Job Description Input & Search Config. 

LAYOUT & CONTROLS:
1. JD Source Toggle: Radio group or tabs to select between 'Upload/Paste JD' (New) and 'Select Existing JD' (Existing).
2. New JD Section: 
   - Text area for pasting JD text (placeholder: 'Paste full job description here...').
   - Optional file upload (PDF/DOCX) that extracts text client-side or via standard backend parser to populate the text area.
3. Existing JD Section:
   - Dropdown populated by querying the 'job_description' collection (display 'job_title' + 'company_name', value '_id').
4. Search Configuration:
   - Number input labeled 'Number of Candidates' (min=1, max=30, required, default=10). Binds to 'candidate_count'.
5. Submit Action: Button labeled 'Configure Search'.

BACKEND HANDLER:
- If 'Select Existing JD': Fetch document from 'job_description' collection by ID. Proceed to output.
- If 'Upload/Paste JD':
  1. Call Gemini LLM with exact prompt:
     "You are an expert HR data extraction assistant. Analyze the provided Job Description text and extract all relevant information into a structured JSON object. Return only the JSON with the following fields — use null for any field that cannot be determined, and return arrays (even empty ones) for all array-type fields:
     {
       "job_title": "Official title or designation of the position",
       "job_category": "Broad functional area or industry domain (e.g., Software Engineering, Accounting)",
       "company_name": "Name of the hiring company or organization",
       "department": "Specific internal team, division, or business unit",
       "location": "Primary physical location (city, state, country)",
       "work_mode": "Work arrangement type (e.g., Remote, Hybrid, Onsite)",
       "employment_type": "Engagement type (e.g., Full-Time, Part-Time, Contract, Internship)",
       "seniority_level": "Career tier (e.g., Entry-Level, Mid-Level, Senior, Lead, Manager, Executive)",
       "min_years_experience": "Minimum required years of relevant experience (number)",
       "max_years_experience": "Maximum target years of experience (number or null if open-ended)",
       "management_experience_required": "Boolean — whether people management or leadership experience is required",
       "required_skills": ["Array of non-negotiable technical, functional, or soft skills"],
       "preferred_skills": ["Array of nice-to-have or optional skills and qualifications"],
       "tools_and_technologies": ["Array of software, platforms, tools, or programming languages required"],
       "education_level": "Minimum academic qualification or degree needed",
       "certifications": ["Array of required or preferred professional certifications and licenses"],
       "key_responsibilities": ["Array of main duties, daily tasks, and role expectations"],
       "domain_expertise": ["Array of specific industry domains or technical practice areas"],
       "salary_min": "Minimum base salary (numeric value or null)",
       "salary_max": "Maximum base salary (numeric value or null)",
       "currency": "3-letter currency code (e.g., USD, INR, EUR) or null",
       "benefits": ["Array of perks, healthcare, allowances, and other work benefits"]
     }
     Job Description Text: {{jd_text}}"
  2. Parse LLM response. If invalid JSON, retry once, then surface error to user.
  3. Insert into 'job_description' collection: { ...extracted_json, jd_text: "<original_text>", created_at: <UTC_timestamp> }.

OUTPUTS:
- jd_id (string: MongoDB ObjectId)
- candidate_count (number)
- All 22 extracted JD fields (job_title, required_skills, etc.) passed forward in workflow state.
```

#### `linkedin_sourcing_tool` description

```
Backend Tool: LinkedIn Sourcing & Scoring Tool. 

INPUTS: jd_id, candidate_count, and structured JD JSON from jd_input_screen.

STEP 2a: GENERATE SEARCH QUERY
Call Gemini LLM with exact prompt:
"You are a LinkedIn recruiting search expert. Given the structured Job Description JSON below, generate an optimized LinkedIn Classic people search query.
Return a JSON object with exactly these fields:
- keywords: A single concise search string (max 200 characters) combining the most important job title, core skills, and domain context. Prioritize specificity over breadth. Do NOT use boolean operators.
- title: The most likely LinkedIn profile headline or current job title that an ideal candidate for this role would have. Keep it short (max 60 characters).
- industry_hint: The single most relevant LinkedIn industry category name for this role (e.g., 'Information Technology and Services', 'Financial Services').
Return only valid JSON. No explanation, no markdown.
JD JSON: {{jd_json}}"

STEP 2b: RESOLVE FILTER IDS
Call Unipile API (api20.unipile.com:15002) with header 'X-API-KEY: {key}':
1. GET /v2/{account_id}/linkedin/search/parameters?type=LOCATION&keyword={jd.location}
2. GET /v2/{account_id}/linkedin/search/parameters?type=INDUSTRY&keyword={gemini.industry_hint}
Extract the first matching 'id' from each response. If no match, omit that filter in Step 2c.

STEP 2c: PAGINATED SEARCH
Loop POST /v2/{account_id}/linkedin/search/people?limit=10&cursor={next_cursor}
Body: {"keywords": "{{gemini.keywords}}", "advanced_keywords": { "title": "{{gemini.title}}" }, "location": ["{{resolved_location_id}}"], "industry": ["{{resolved_industry_id}}"], "network_distance": [2, 3]}
Stop when collected profiles >= candidate_count OR next_cursor is absent OR 5 pages (50 profiles) reached. Trim array to exact candidate_count.

STEP 2d: FETCH DETAILED PROFILES
For each profile, call GET /api/v1/users/{public_identifier}?account_id={account_id}&linkedin_sections=about,experience,education,skills,certifications
Execution: Async parallel, batch size 5, random delay 0.5-1s between batches. If response contains 'throttled_sections[]', retry that specific profile once after a 2s delay.

STEP 2e: SCHEMA MAPPING
Map to 'all_Candidates' collection schema. Unexposed fields must be explicit nulls.
- file_name, resume_file_hash, resume_file_link, why_interested_in_role, current_notice_period, earliest_join_date, comfortable_working, current_compensation, compensation_expectations, competing_offers, willing_to_relocate = null
- processed_at = UTC now
- email = contact_info.emails[0] (or null)
- phone = contact_info.phones[0] (or null)
- first_name = first_name, last_name = last_name, location = location, current_base = location
- total_years_of_experience = sum of experience durations (decimal, 1 place)
- summary = about
- skills = skills[].name
- education = education[] mapped to {degree, institution, year}
- experience = work_experience[] mapped to {title, company, start_date, end_date, description}
- source = "linkedin", status = "linkedin_sourced"
- actively_looking = "Yes" if is_open_to_work == true else "No"
- linkedin_profile_url = profile_url, linkedin_public_identifier = public_identifier, linkedin_provider_id = provider_id, headline = headline, profile_picture_url = profile_picture_url, network_distance = network_distance, certifications = certifications[], jd_id = jd_id.

STEP 2f: SCORING (Rule-Based)
1. Skills (30 pts): jd_skills = jd.required_skills + jd.tools_and_technologies + jd.preferred_skills. candidate_text = skills[].name + work_experience[].skills + work_experience[].description + headline (all lowercase). Check substring matches. skills_score = (matched_count / total jd_skills count) * 30 (cap 30). Save to matched_skills[].
2. Experience (30 pts): Sum durations. If jd.min_years_experience is null OR total_years >= jd.min_years_experience -> 30. Else -> (total_years / jd.min_years_experience) * 30.
3. Education (20 pts): Tiers: PhD=5, Master's=4, Bachelor's=3, Diploma=2, High School=1, None=0. gap = jd_tier - candidate_tier. gap<=0 -> 20, gap==1 -> 14, gap==2 -> 7, gap>=3 -> 0. If jd.education_level is null -> 20.
4. Open to Work (10 pts): is_open_to_work == true -> 10, else 0.
5. Contact (10 pts): email present -> 5, phone present -> 5.
Total score = sum of above. Save 'score' and 'score_breakdown' object.

STEP 2g: DEDUPLICATE & SAVE
Upsert each profile into 'all_Candidates' collection matching on 'linkedin_public_identifier'. Sort final array by score descending.

OUTPUTS: batch_id (string, equals jd_id), fetched_count (number).
```

#### `candidate_profiles_screen` description

```
UI Screen: Candidate Profiles UI. 

DATA FETCHING:
Backend handler queries the 'all_Candidates' collection where `jd_id` == `batch_id` (passed from previous node). Sort results by `score` descending by default.

LIST-LEVEL UI:
1. Header: Display "Showing {fetched_count} of {candidate_count} requested" (e.g., 'Showing 30 of 30 requested').
2. Controls Bar:
   - Sort Toggle: Dropdown to sort by 'Score' (default), 'Experience' (total_years_of_experience), or 'Network Distance'.
   - Filter: Number input or slider for 'Minimum Score Threshold' (0-100) that dynamically filters the rendered list.

PROFILE CARD LAYOUT (Rendered for each candidate):
1. Header Area:
   - `profile_picture_url` (circular avatar, fallback to initials if null).
   - Full Name (`first_name` + `last_name`).
   - LinkedIn Icon linking to `linkedin_profile_url` (target=_blank).
   - `headline` (subtext) and `location` (subtext).
2. Scoring Area:
   - Score Badge: Display `score / 100`. Color logic: Green if >= 70, Orange if 40-69, Red if < 40.
   - Score Breakdown Bar: A segmented visual progress bar showing the 5 components from `score_breakdown` (Skills, Experience, Education, Open to Work, Contact).
3. Key Highlights:
   - Total years of experience: Display `total_years_of_experience` + " Years".
   - Open to Work Badge: Render a prominent badge only if `actively_looking` == "Yes".
   - Matched Skills: Render `matched_skills` array as highlighted chips/tags.
4. Contact Info:
   - Display `email` and `phone` with copy-to-clipboard icons. If null, display "Not provided".
5. Experience Summary:
   - List the top 2-3 most recent entries from the `experience` array (format: Company + Position + Duration).

ACTIONS:
- Primary Submit Button: Labeled 'Finish Review'.
- On submit, transition to end node.

OUTPUTS:
- review_completed (boolean: true)
```

### `stage4c`

- **`jd_input_screen`** (ui_screen) chars=3642 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`
- **`linkedin_sourcing_tool`** (tool) chars=4597 fidelity=`{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}`
- **`candidate_profiles_screen`** (ui_screen) chars=1840 fidelity=`{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`

#### `jd_input_screen` description

```
UI Screen: Job Description Input & Search Config. 

LAYOUT & CONTROLS:
1. JD Source Toggle: Radio group or tabs to select between 'Upload/Paste JD' (New) and 'Select Existing JD' (Existing).
2. New JD Section: 
   - Text area for pasting JD text (placeholder: 'Paste full job description here...').
   - Optional file upload (PDF/DOCX) that extracts text client-side or via standard backend parser to populate the text area.
3. Existing JD Section:
   - Dropdown populated by querying the 'job_description' collection (display 'job_title' + 'company_name', value '_id').
4. Search Configuration:
   - Number input labeled 'Number of Candidates' (min=1, max=30, required, default=10). Binds to 'candidate_count'.
5. Submit Action: Button labeled 'Configure Search'.

BACKEND HANDLER:
- If 'Select Existing JD': Fetch document from 'job_description' collection by ID. Proceed to output.
- If 'Upload/Paste JD':
  1. Call Gemini LLM with exact prompt:
     "You are an expert HR data extraction assistant. Analyze the provided Job Description text and extract all relevant information into a structured JSON object. Return only the JSON with the following fields — use null for any field that cannot be determined, and return arrays (even empty ones) for all array-type fields:
     {
       "job_title": "Official title or designation of the position",
       "job_category": "Broad functional area or industry domain (e.g., Software Engineering, Accounting)",
       "company_name": "Name of the hiring company or organization",
       "department": "Specific internal team, division, or business unit",
       "location": "Primary physical location (city, state, country)",
       "work_mode": "Work arrangement type (e.g., Remote, Hybrid, Onsite)",
       "employment_type": "Engagement type (e.g., Full-Time, Part-Time, Contract, Internship)",
       "seniority_level": "Career tier (e.g., Entry-Level, Mid-Level, Senior, Lead, Manager, Executive)",
       "min_years_experience": "Minimum required years of relevant experience (number)",
       "max_years_experience": "Maximum target years of experience (number or null if open-ended)",
       "management_experience_required": "Boolean — whether people management or leadership experience is required",
       "required_skills": ["Array of non-negotiable technical, functional, or soft skills"],
       "preferred_skills": ["Array of nice-to-have or optional skills and qualifications"],
       "tools_and_technologies": ["Array of software, platforms, tools, or programming languages required"],
       "education_level": "Minimum academic qualification or degree needed",
       "certifications": ["Array of required or preferred professional certifications and licenses"],
       "key_responsibilities": ["Array of main duties, daily tasks, and role expectations"],
       "domain_expertise": ["Array of specific industry domains or technical practice areas"],
       "salary_min": "Minimum base salary (numeric value or null)",
       "salary_max": "Maximum base salary (numeric value or null)",
       "currency": "3-letter currency code (e.g., USD, INR, EUR) or null",
       "benefits": ["Array of perks, healthcare, allowances, and other work benefits"]
     }
     Job Description Text: {{jd_text}}"
  2. Parse LLM response. If invalid JSON, retry once, then surface error to user.
  3. Insert into 'job_description' collection: { ...extracted_json, jd_text: "<original_text>", created_at: <UTC_timestamp> }.

OUTPUTS:
- jd_id (string: MongoDB ObjectId)
- candidate_count (number)
- All 22 extracted JD fields (job_title, required_skills, etc.) passed forward in workflow state.
```

#### `linkedin_sourcing_tool` description

```
Backend Tool: LinkedIn Sourcing & Scoring Tool. 

INPUTS: jd_id, candidate_count, and structured JD JSON from jd_input_screen.

STEP 2a: GENERATE SEARCH QUERY
Call Gemini LLM with exact prompt:
"You are a LinkedIn recruiting search expert. Given the structured Job Description JSON below, generate an optimized LinkedIn Classic people search query.
Return a JSON object with exactly these fields:
- keywords: A single concise search string (max 200 characters) combining the most important job title, core skills, and domain context. Prioritize specificity over breadth. Do NOT use boolean operators.
- title: The most likely LinkedIn profile headline or current job title that an ideal candidate for this role would have. Keep it short (max 60 characters).
- industry_hint: The single most relevant LinkedIn industry category name for this role (e.g., 'Information Technology and Services', 'Financial Services').
Return only valid JSON. No explanation, no markdown.
JD JSON: {{jd_json}}"

STEP 2b: RESOLVE FILTER IDS
Call Unipile API (api20.unipile.com:15002) with header 'X-API-KEY: {key}':
1. GET /v2/{account_id}/linkedin/search/parameters?type=LOCATION&keyword={jd.location}
2. GET /v2/{account_id}/linkedin/search/parameters?type=INDUSTRY&keyword={gemini.industry_hint}
Extract the first matching 'id' from each response. If no match, omit that filter in Step 2c.

STEP 2c: PAGINATED SEARCH
Loop POST /v2/{account_id}/linkedin/search/people?limit=10&cursor={next_cursor}
Body: {"keywords": "{{gemini.keywords}}", "advanced_keywords": { "title": "{{gemini.title}}" }, "location": ["{{resolved_location_id}}"], "industry": ["{{resolved_industry_id}}"], "network_distance": [2, 3]}
Stop when collected profiles >= candidate_count OR next_cursor is absent OR 5 pages (50 profiles) reached. Trim array to exact candidate_count.

STEP 2d: FETCH DETAILED PROFILES
For each profile, call GET /api/v1/users/{public_identifier}?account_id={account_id}&linkedin_sections=about,experience,education,skills,certifications
Execution: Async parallel, batch size 5, random delay 0.5-1s between batches. If response contains 'throttled_sections[]', retry that specific profile once after a 2s delay.

STEP 2e: SCHEMA MAPPING
Map to 'all_Candidates' collection schema. Unexposed fields must be explicit nulls.
- file_name, resume_file_hash, resume_file_link, why_interested_in_role, current_notice_period, earliest_join_date, comfortable_working, current_compensation, compensation_expectations, competing_offers, willing_to_relocate = null
- processed_at = UTC now
- email = contact_info.emails[0] (or null)
- phone = contact_info.phones[0] (or null)
- first_name = first_name, last_name = last_name, location = location, current_base = location
- total_years_of_experience = sum of experience durations (decimal, 1 place)
- summary = about
- skills = skills[].name
- education = education[] mapped to {degree, institution, year}
- experience = work_experience[] mapped to {title, company, start_date, end_date, description}
- source = "linkedin", status = "linkedin_sourced"
- actively_looking = "Yes" if is_open_to_work == true else "No"
- linkedin_profile_url = profile_url, linkedin_public_identifier = public_identifier, linkedin_provider_id = provider_id, headline = headline, profile_picture_url = profile_picture_url, network_distance = network_distance, certifications = certifications[], jd_id = jd_id.

STEP 2f: SCORING (Rule-Based)
1. Skills (30 pts): jd_skills = jd.required_skills + jd.tools_and_technologies + jd.preferred_skills. candidate_text = skills[].name + work_experience[].skills + work_experience[].description + headline (all lowercase). Check substring matches. skills_score = (matched_count / total jd_skills count) * 30 (cap 30). Save to matched_skills[].
2. Experience (30 pts): Sum durations. If jd.min_years_experience is null OR total_years >= jd.min_years_experience -> 30. Else -> (total_years / jd.min_years_experience) * 30.
3. Education (20 pts): Tiers: PhD=5, Master's=4, Bachelor's=3, Diploma=2, High School=1, None=0. gap = jd_tier - candidate_tier. gap<=0 -> 20, gap==1 -> 14, gap==2 -> 7, gap>=3 -> 0. If jd.education_level is null -> 20.
4. Open to Work (10 pts): is_open_to_work == true -> 10, else 0.
5. Contact (10 pts): email present -> 5, phone present -> 5.
Total score = sum of above. Save 'score' and 'score_breakdown' object.

STEP 2g: DEDUPLICATE & SAVE
Upsert each profile into 'all_Candidates' collection matching on 'linkedin_public_identifier'. Sort final array by score descending.

OUTPUTS: batch_id (string, equals jd_id), fetched_count (number).
```

#### `candidate_profiles_screen` description

```
UI Screen: Candidate Profiles UI. 

DATA FETCHING:
Backend handler queries the 'all_Candidates' collection where `jd_id` == `batch_id` (passed from previous node). Sort results by `score` descending by default.

LIST-LEVEL UI:
1. Header: Display "Showing {fetched_count} of {candidate_count} requested" (e.g., 'Showing 30 of 30 requested').
2. Controls Bar:
   - Sort Toggle: Dropdown to sort by 'Score' (default), 'Experience' (total_years_of_experience), or 'Network Distance'.
   - Filter: Number input or slider for 'Minimum Score Threshold' (0-100) that dynamically filters the rendered list.

PROFILE CARD LAYOUT (Rendered for each candidate):
1. Header Area:
   - `profile_picture_url` (circular avatar, fallback to initials if null).
   - Full Name (`first_name` + `last_name`).
   - LinkedIn Icon linking to `linkedin_profile_url` (target=_blank).
   - `headline` (subtext) and `location` (subtext).
2. Scoring Area:
   - Score Badge: Display `score / 100`. Color logic: Green if >= 70, Orange if 40-69, Red if < 40.
   - Score Breakdown Bar: A segmented visual progress bar showing the 5 components from `score_breakdown` (Skills, Experience, Education, Open to Work, Contact).
3. Key Highlights:
   - Total years of experience: Display `total_years_of_experience` + " Years".
   - Open to Work Badge: Render a prominent badge only if `actively_looking` == "Yes".
   - Matched Skills: Render `matched_skills` array as highlighted chips/tags.
4. Contact Info:
   - Display `email` and `phone` with copy-to-clipboard icons. If null, display "Not provided".
5. Experience Summary:
   - List the top 2-3 most recent entries from the `experience` array (format: Company + Position + Duration).

ACTIONS:
- Primary Submit Button: Labeled 'Finish Review'.
- On submit, transition to end node.

OUTPUTS:
- review_completed (boolean: true)
```

## 1b. Final graph `intentDescription` / `generationDescription`

### `jd_input_screen` (ui_screen) — Job Description Input & Search Config

| Field | Chars | Fidelity |
|---|---|---|
| intentDescription | 3642 | `{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}` |
| generationDescription | 3642 | `{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}` |
| generation.description | 3642 | `{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}` |

<details><summary>intentDescription</summary>

```
UI Screen: Job Description Input & Search Config. 

LAYOUT & CONTROLS:
1. JD Source Toggle: Radio group or tabs to select between 'Upload/Paste JD' (New) and 'Select Existing JD' (Existing).
2. New JD Section: 
   - Text area for pasting JD text (placeholder: 'Paste full job description here...').
   - Optional file upload (PDF/DOCX) that extracts text client-side or via standard backend parser to populate the text area.
3. Existing JD Section:
   - Dropdown populated by querying the 'job_description' collection (display 'job_title' + 'company_name', value '_id').
4. Search Configuration:
   - Number input labeled 'Number of Candidates' (min=1, max=30, required, default=10). Binds to 'candidate_count'.
5. Submit Action: Button labeled 'Configure Search'.

BACKEND HANDLER:
- If 'Select Existing JD': Fetch document from 'job_description' collection by ID. Proceed to output.
- If 'Upload/Paste JD':
  1. Call Gemini LLM with exact prompt:
     "You are an expert HR data extraction assistant. Analyze the provided Job Description text and extract all relevant information into a structured JSON object. Return only the JSON with the following fields — use null for any field that cannot be determined, and return arrays (even empty ones) for all array-type fields:
     {
       "job_title": "Official title or designation of the position",
       "job_category": "Broad functional area or industry domain (e.g., Software Engineering, Accounting)",
       "company_name": "Name of the hiring company or organization",
       "department": "Specific internal team, division, or business unit",
       "location": "Primary physical location (city, state, country)",
       "work_mode": "Work arrangement type (e.g., Remote, Hybrid, Onsite)",
       "employment_type": "Engagement type (e.g., Full-Time, Part-Time, Contract, Internship)",
       "seniority_level": "Career tier (e.g., Entry-Level, Mid-Level, Senior, Lead, Manager, Executive)",
       "min_years_experience": "Minimum required years of relevant experience (number)",
       "max_years_experience": "Maximum target years of experience (number or null if open-ended)",
       "management_experience_required": "Boolean — whether people management or leadership experience is required",
       "required_skills": ["Array of non-negotiable technical, functional, or soft skills"],
       "preferred_skills": ["Array of nice-to-have or optional skills and qualifications"],
       "tools_and_technologies": ["Array of software, platforms, tools, or programming languages required"],
       "education_level": "Minimum academic qualification or degree needed",
       "certifications": ["Array of required or preferred professional certifications and licenses"],
       "key_responsibilities": ["Array of main duties, daily tasks, and role expectations"],
       "domain_expertise": ["Array of specific industry domains or technical practice areas"],
       "salary_min": "Minimum base salary (numeric value or null)",
       "salary_max": "Maximum base salary (numeric value or null)",
       "currency": "3-letter currency code (e.g., USD, INR, EUR) or null",
       "benefits": ["Array of perks, healthcare, allowances, and other work benefits"]
     }
     Job Description Text: {{jd_text}}"
  2. Parse LLM response. If invalid JSON, retry once, then surface error to user.
  3. Insert into 'job_description' collection: { ...extracted_json, jd_text: "<original_text>", created_at: <UTC_timestamp> }.

OUTPUTS:
- jd_id (string: MongoDB ObjectId)
- candidate_count (number)
- All 22 extracted JD fields (job_title, required_skills, etc.) passed forward in workflow state.
```
</details>

### `linkedin_sourcing_tool` (tool) — LinkedIn Sourcing & Scoring Tool

| Field | Chars | Fidelity |
|---|---|---|
| intentDescription | 4597 | `{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}` |
| generationDescription | 4597 | `{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}` |
| generation.description | 4597 | `{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}` |

<details><summary>intentDescription</summary>

```
Backend Tool: LinkedIn Sourcing & Scoring Tool. 

INPUTS: jd_id, candidate_count, and structured JD JSON from jd_input_screen.

STEP 2a: GENERATE SEARCH QUERY
Call Gemini LLM with exact prompt:
"You are a LinkedIn recruiting search expert. Given the structured Job Description JSON below, generate an optimized LinkedIn Classic people search query.
Return a JSON object with exactly these fields:
- keywords: A single concise search string (max 200 characters) combining the most important job title, core skills, and domain context. Prioritize specificity over breadth. Do NOT use boolean operators.
- title: The most likely LinkedIn profile headline or current job title that an ideal candidate for this role would have. Keep it short (max 60 characters).
- industry_hint: The single most relevant LinkedIn industry category name for this role (e.g., 'Information Technology and Services', 'Financial Services').
Return only valid JSON. No explanation, no markdown.
JD JSON: {{jd_json}}"

STEP 2b: RESOLVE FILTER IDS
Call Unipile API (api20.unipile.com:15002) with header 'X-API-KEY: {key}':
1. GET /v2/{account_id}/linkedin/search/parameters?type=LOCATION&keyword={jd.location}
2. GET /v2/{account_id}/linkedin/search/parameters?type=INDUSTRY&keyword={gemini.industry_hint}
Extract the first matching 'id' from each response. If no match, omit that filter in Step 2c.

STEP 2c: PAGINATED SEARCH
Loop POST /v2/{account_id}/linkedin/search/people?limit=10&cursor={next_cursor}
Body: {"keywords": "{{gemini.keywords}}", "advanced_keywords": { "title": "{{gemini.title}}" }, "location": ["{{resolved_location_id}}"], "industry": ["{{resolved_industry_id}}"], "network_distance": [2, 3]}
Stop when collected profiles >= candidate_count OR next_cursor is absent OR 5 pages (50 profiles) reached. Trim array to exact candidate_count.

STEP 2d: FETCH DETAILED PROFILES
For each profile, call GET /api/v1/users/{public_identifier}?account_id={account_id}&linkedin_sections=about,experience,education,skills,certifications
Execution: Async parallel, batch size 5, random delay 0.5-1s between batches. If response contains 'throttled_sections[]', retry that specific profile once after a 2s delay.

STEP 2e: SCHEMA MAPPING
Map to 'all_Candidates' collection schema. Unexposed fields must be explicit nulls.
- file_name, resume_file_hash, resume_file_link, why_interested_in_role, current_notice_period, earliest_join_date, comfortable_working, current_compensation, compensation_expectations, competing_offers, willing_to_relocate = null
- processed_at = UTC now
- email = contact_info.emails[0] (or null)
- phone = contact_info.phones[0] (or null)
- first_name = first_name, last_name = last_name, location = location, current_base = location
- total_years_of_experience = sum of experience durations (decimal, 1 place)
- summary = about
- skills = skills[].name
- education = education[] mapped to {degree, institution, year}
- experience = work_experience[] mapped to {title, company, start_date, end_date, description}
- source = "linkedin", status = "linkedin_sourced"
- actively_looking = "Yes" if is_open_to_work == true else "No"
- linkedin_profile_url = profile_url, linkedin_public_identifier = public_identifier, linkedin_provider_id = provider_id, headline = headline, profile_picture_url = profile_picture_url, network_distance = network_distance, certifications = certifications[], jd_id = jd_id.

STEP 2f: SCORING (Rule-Based)
1. Skills (30 pts): jd_skills = jd.required_skills + jd.tools_and_technologies + jd.preferred_skills. candidate_text = skills[].name + work_experience[].skills + work_experience[].description + headline (all lowercase). Check substring matches. skills_score = (matched_count / total jd_skills count) * 30 (cap 30). Save to matched_skills[].
2. Experience (30 pts): Sum durations. If jd.min_years_experience is null OR total_years >= jd.min_years_experience -> 30. Else -> (total_years / jd.min_years_experience) * 30.
3. Education (20 pts): Tiers: PhD=5, Master's=4, Bachelor's=3, Diploma=2, High School=1, None=0. gap = jd_tier - candidate_tier. gap<=0 -> 20, gap==1 -> 14, gap==2 -> 7, gap>=3 -> 0. If jd.education_level is null -> 20.
4. Open to Work (10 pts): is_open_to_work == true -> 10, else 0.
5. Contact (10 pts): email present -> 5, phone present -> 5.
Total score = sum of above. Save 'score' and 'score_breakdown' object.

STEP 2g: DEDUPLICATE & SAVE
Upsert each profile into 'all_Candidates' collection matching on 'linkedin_public_identifier'. Sort final array by score descending.

OUTPUTS: batch_id (string, equals jd_id), fetched_count (number).
```
</details>

### `candidate_profiles_screen` (ui_screen) — Candidate Profiles UI

| Field | Chars | Fidelity |
|---|---|---|
| intentDescription | 1840 | `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}` |
| generationDescription | 1840 | `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}` |
| generation.description | 1840 | `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}` |

<details><summary>intentDescription</summary>

```
UI Screen: Candidate Profiles UI. 

DATA FETCHING:
Backend handler queries the 'all_Candidates' collection where `jd_id` == `batch_id` (passed from previous node). Sort results by `score` descending by default.

LIST-LEVEL UI:
1. Header: Display "Showing {fetched_count} of {candidate_count} requested" (e.g., 'Showing 30 of 30 requested').
2. Controls Bar:
   - Sort Toggle: Dropdown to sort by 'Score' (default), 'Experience' (total_years_of_experience), or 'Network Distance'.
   - Filter: Number input or slider for 'Minimum Score Threshold' (0-100) that dynamically filters the rendered list.

PROFILE CARD LAYOUT (Rendered for each candidate):
1. Header Area:
   - `profile_picture_url` (circular avatar, fallback to initials if null).
   - Full Name (`first_name` + `last_name`).
   - LinkedIn Icon linking to `linkedin_profile_url` (target=_blank).
   - `headline` (subtext) and `location` (subtext).
2. Scoring Area:
   - Score Badge: Display `score / 100`. Color logic: Green if >= 70, Orange if 40-69, Red if < 40.
   - Score Breakdown Bar: A segmented visual progress bar showing the 5 components from `score_breakdown` (Skills, Experience, Education, Open to Work, Contact).
3. Key Highlights:
   - Total years of experience: Display `total_years_of_experience` + " Years".
   - Open to Work Badge: Render a prominent badge only if `actively_looking` == "Yes".
   - Matched Skills: Render `matched_skills` array as highlighted chips/tags.
4. Contact Info:
   - Display `email` and `phone` with copy-to-clipboard icons. If null, display "Not provided".
5. Experience Summary:
   - List the top 2-3 most recent entries from the `experience` array (format: Company + Position + Duration).

ACTIONS:
- Primary Submit Button: Labeled 'Finish Review'.
- On submit, transition to end node.

OUTPUTS:
- review_completed (boolean: true)
```
</details>

## 2. Gap UI → AIProcessor (enhance + IR + databaseLogic)

- node: `candidate_profiles_screen`
- intent chars → enhanced chars: 1840 → 8629
- original intent still present in enhanced (substring check): **True**
- fidelity intent: `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`
- fidelity enhanced: `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`

### Enhanced prompt preview

```
ORIGINAL NODE SPECIFICATION (verbatim — every fact here is authoritative and must be implemented; later sections may only add detail):
UI Screen: Candidate Profiles UI. 

DATA FETCHING:
Backend handler queries the 'all_Candidates' collection where `jd_id` == `batch_id` (passed from previous node). Sort results by `score` descending by default.

LIST-LEVEL UI:
1. Header: Display "Showing {fetched_count} of {candidate_count} requested" (e.g., 'Showing 30 of 30 requested').
2. Controls Bar:
   - Sort Toggle: Dropdown to sort by 'Score' (default), 'Experience' (total_years_of_experience), or 'Network Distance'.
   - Filter: Number input or slider for 'Minimum Score Threshold' (0-100) that dynamically filters the rendered list.

PROFILE CARD LAYOUT (Rendered for each candidate):
1. Header Area:
   - `profile_picture_url` (circular avatar, fallback to initials if null).
   - Full Name (`first_name` + `last_name`).
   - LinkedIn Icon linking to `linkedin_profile_url` (target=_blank).
   - `headline` (subtext) and `location` (subtext).
2. Scoring Area:
   - Score Badge: Display `score / 100`. Color logic: Green if >= 70, Orange if 40-69, Red if < 40.
   - Score Breakdown Bar: A segmented visual progress bar showing the 5 components from `score_breakdown` (Skills, Experience, Education, Open to Work, Contact).
3. Key Highlights:
   - Total years of experience: Display `total_years_of_experience` + " Years".
   - Open to Work Badge: Render a prominent badge only if `actively_looking` == "Yes".
   - Matched Skills: Render `matched_skills` array as highlighted chips/tags.
4. Contact Info:
   - Display `email` and `phone` with copy-to-clipboard icons. If null, display "Not provided".
5. Experience Summary:
   - List the top 2-3 most recent entries from the `experience` array (format: Company + Position + Duration).

ACTIONS:
- Primary Submit Button: Labeled 'Finish Review'.
- On submit, transition to end node.

OUTPUTS:
- review_completed (boolean: true)

=== WORKFLOW IO CONTRACT (SCREEN) ===
{
  "inputParameters": [
    {
      "name": "batch_id",
      "dataType": "string",
      "required": true
    },
    {
      "name": "fetched_count",
      "dataType": "number",
      "required": true
    },
    {
      "name": "candidate_count",
      "dataType": "number",
      "required": true
    }
  ],
  "outputParameters": [
    {
      "name": "review_completed",
      "dataType": "boolean",
      "required": true
    }
  ]
}

ENHANCED / ELABORATED REQUIREMENTS (additive 
```

### DataSources after IR + enricher

- **`fetch_candidates`** type=`dynamic` desc=164c logic=1376c
  <details><summary>databaseLogic</summary>

  ```
from services.database import Database

1. Validate `params.batch_id` is provided. If missing, return `{"error": "batch_id is required"}`.
2. Runtime: inspect the `all_Candidates` collection to discover actual field names before querying. Resolve casing (e.g., jd_id vs jdId, score vs Score, total_years_of_experience vs totalYearsOfExperience) from the live schema. Do NOT assume field names.
3. Construct the query filter: match the resolved `jd_id` field to `params.batch_id`.
4. If `params.min_score` is provided (0-100), append a filter condition where the resolved `score` field is `$gte` `params.min_score`.
5. Determine sorting logic based on `params.sort_by`:
   - If 'Experience', sort by resolved `total_years_of_experience` DESC.
   - If 'Network Distance', sort by resolved `network_distance` ASC.
   - Default: sort by resolved `score` DESC.
6. Execute `await Database.find("all_Candidates", query, sort=sort_logic)`.
7. Map the raw documents to the `Candidate` responseModel schema. Ensure nested objects like `score_breakdown` and arrays like `matched_skills` and `experience` are properly mapped. Handle nulls gracefully for optional fields (`profile_picture_url`, `linkedin_profile_url`, `headline`, `location`, `email`, `phone`).
8. Return `{"candidates": mapped_results}`. On database timeout or failure, catch the exception and return `{"error": str(e)}`.
  ```
  </details>

- **`submit_review`** type=`dynamic` desc=133c logic=730c
  <details><summary>databaseLogic</summary>

  ```
from services.database import Database
from datetime import datetime, timezone

1. Validate `params.batch_id` and `params.review_completed` are provided. If missing, return `{"success": False, "error": "Missing required parameters"}`.
2. Construct the update payload as a plain dictionary (per platform rules, never use {"$set": ...}):
   `payload = {"batch_id": params.batch_id, "review_completed": params.review_completed, "updated_at": datetime.now(timezone.utc).isoformat()}`.
3. Execute `await Database.update_one("batch_reviews", {"batch_id": params.batch_id}, payload, upsert=True)`.
4. Return `{"success": True}`. On database error or timeout, catch the exception, log it, and return `{"success": False, "error": str(e)}`.
  ```
  </details>

- IR screen: `{"id": "candidate_profiles_screen", "type": "form", "title": "Candidate Profiles Dashboard", "description": "High-density data visualization and review interface for candidate profiles associated with a specific job description batch.", "route": "/candidates/review", "selector": "app-candidate-profiles"}`

## 3. Gap tool generator

- node: `linkedin_sourcing_tool`
- intent fidelity: `{"unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "matched_skills": true}`
- generated function mentions Unipile: **False**
- mentions api20: **False**
- mentions linkedin_sections: **False**
- mentions scoring: **False**
- function_code chars: 0
- parse_error: None

<details><summary>function_code preview</summary>

```python

```
</details>
## 4. Backend generator path (grounding / databaseLogic)

- **`fetch_candidates`** type=`dynamic` has_endpoint=False logic_chars=1376
  - grounding: _(not needed or empty)_

  <details><summary>databaseLogic preview</summary>

  ```
from services.database import Database

1. Validate `params.batch_id` is provided. If missing, return `{"error": "batch_id is required"}`.
2. Runtime: inspect the `all_Candidates` collection to discover actual field names before querying. Resolve casing (e.g., jd_id vs jdId, score vs Score, total_years_of_experience vs totalYearsOfExperience) from the live schema. Do NOT assume field names.
3. Construct the query filter: match the resolved `jd_id` field to `params.batch_id`.
4. If `params.min_score` is provided (0-100), append a filter condition where the resolved `score` field is `$gte` `params.min_score`.
5. Determine sorting logic based on `params.sort_by`:
   - If 'Experience', sort by resolved `total_years_of_experience` DESC.
   - If 'Network Distance', sort by resolved `network_distance` ASC.
   - Default: sort by resolved `score` DESC.
6. Execute `await Database.find("all_Candidates", query, sort=sort_logic)`.
7. Map the raw documents to the `Candidate` responseModel schema. Ensure nested objects like `score_breakdown` and arrays like `matched_skills` and `experience` are properly mapped. Handle nulls gracefully for optional fields (`profile_picture_url`, `linkedin_profile_url`, `headline`, `location`, `email`, `phone`).
8. Return `{"candidates": mapped_results}`. On database timeout or failure, catch the exception and return `{"error": str(e)}`.
  ```
  </details>

- **`submit_review`** type=`dynamic` has_endpoint=False logic_chars=730
  - grounding: _(not needed or empty)_

  <details><summary>databaseLogic preview</summary>

  ```
from services.database import Database
from datetime import datetime, timezone

1. Validate `params.batch_id` and `params.review_completed` are provided. If missing, return `{"success": False, "error": "Missing required parameters"}`.
2. Construct the update payload as a plain dictionary (per platform rules, never use {"$set": ...}):
   `payload = {"batch_id": params.batch_id, "review_completed": params.review_completed, "updated_at": datetime.now(timezone.utc).isoformat()}`.
3. Execute `await Database.update_one("batch_reviews", {"batch_id": params.batch_id}, payload, upsert=True)`.
4. Return `{"success": True}`. On database error or timeout, catch the exception, log it, and return `{"success": False, "error": str(e)}`.
  ```
  </details>
## 5. Verdict checklist (Issue 1)

| Check | Pass | Notes |
|---|---|---|
| UI intent retains profile cards / score / skills / sort | ✅ | from primary run |
| Enhanced prompt keeps original (substring) | ✅ | ORIGINAL NODE SPECIFICATION prepended |
| Tool intent has Unipile/API | ✅ | |
| Tool intent has scoring weights | ✅ | |
| Generated tool code mentions Unipile or api20 | ❌ | |
| Generated tool mentions linkedin_sections | ❌ | |
| Generated tool mentions scoring | ❌ | |

## Artifacts

Primary run dir: `/home/user/Projects/screen-builder/backend/scripts/eval_output/linkedin_intent_trace_20260915_074319`

Continuation filled sections 3–4 after fixing import mocks (`crewai`, `Database`).

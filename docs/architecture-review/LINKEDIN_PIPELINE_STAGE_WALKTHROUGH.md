# LinkedIn Pipeline — Stage-by-Stage Walkthrough (Live Trace)

_Generated from live artifacts:_ `backend/scripts/eval_output/linkedin_intent_trace_20260915_074319/`

_Source report:_ `docs/architecture-review/LINKEDIN_INTENT_FIDELITY_LIVE_TRACE.md`

_Runners:_ `run_linkedin_intent_fidelity_live_trace.py` + `continue_linkedin_intent_trace_tool_backend.py`

This document preserves the **actual** texts produced at each pipeline stage for the LinkedIn Recruitment sub-flow (3 process steps → 4 graph nodes including `end`).

---

## Table of contents

1. [Original user prompt (3 steps)](#1-original-user-prompt-3-steps)
2. [Stage 1 — Skeleton graph creator](#2-stage-1--skeleton-graph-creator)
3. [Stage 1.2 — Description enricher](#3-stage-12--description-enricher)
4. [Stage 1.7 / Stage 2a — Schema builder (graph becomes typed)](#4-stage-17--stage-2a--schema-builder-graph-becomes-typed)
5. [Stage 4c — Persistence + intent freeze](#5-stage-4c--persistence--intent-freeze)
6. [Gap tool creator](#6-gap-tool-creator)
7. [Gap UI screen → AIProcessor (enhance + IR + dataSource enricher)](#7-gap-ui-screen--aiprocessor-enhance--ir--datasource-enricher)
8. [Backend generator path](#8-backend-generator-path)
9. [Char-length transformation summary](#9-char-length-transformation-summary)

---

## 1. Original user prompt (3 steps)

Source: LinkedIn Recruitment process config (`linkedin-recruiter/Process_configs.txt` subset used in the live trace).

**Step count: 3** process steps (mapped to preferred kinds below). The skeleton planner also emits a 4th `end` node.

| # | Process step name | stepId | preferredKind | chars |
|---|---|---|---|---:|
| 1 | Screen 1 | `row_keetc3e3q` | `ui_screen` | 3189 |
| 2 | Tool 1 | `row_toc1mxpz8` | `tool` | 7947 |
| 3 | Screen 2 | `row_o5h4okahg` | `ui_screen` | 829 |

Fidelity signal snapshot (from `00_original_steps.json`):

```json
[
  {
    "stepId": "row_keetc3e3q",
    "name": "Screen 1",
    "preferredKind": "ui_screen",
    "chars": 3189,
    "fidelity": {
      "profile_cards_or_photo": false,
      "score_badge_or_breakdown": false,
      "matched_skills": false,
      "sort_or_filter": false,
      "unipile_or_api20": false,
      "linkedin_sections": false,
      "scoring_weights": false,
      "all_candidates_collection": false,
      "candidate_count_max_30": true
    }
  },
  {
    "stepId": "row_toc1mxpz8",
    "name": "Tool 1",
    "preferredKind": "tool",
    "chars": 7947,
    "fidelity": {
      "profile_cards_or_photo": true,
      "score_badge_or_breakdown": true,
      "matched_skills": true,
      "sort_or_filter": true,
      "unipile_or_api20": true,
      "linkedin_sections": true,
      "scoring_weights": true,
      "all_candidates_collection": true,
      "candidate_count_max_30": true
    }
  },
  {
    "stepId": "row_o5h4okahg",
    "name": "Screen 2",
    "preferredKind": "ui_screen",
    "chars": 829,
    "fidelity": {
      "profile_cards_or_photo": true,
      "score_badge_or_breakdown": true,
      "matched_skills": true,
      "sort_or_filter": true,
      "unipile_or_api20": false,
      "linkedin_sections": false,
      "scoring_weights": true,
      "all_candidates_collection": false,
      "candidate_count_max_30": false
    }
  }
]
```


### Step 1 — Screen 1 / Job Description Input (`row_keetc3e3q`, ui_screen, 3189 chars)

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


### Step 2 — Tool 1 / Profile Fetching, Enrichment, Scoring & Storage (`row_toc1mxpz8`, tool, 7947 chars)

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


### Step 3 — Screen 2 / Display Profiles in UI (`row_o5h4okahg`, ui_screen, 829 chars)

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


> Note: The live-trace executive summary listed Screen 3 as 1037 chars; the authoritative original block in the same report is **829 chars** (`00_original_steps.json`).

---

## 2. Stage 1 — Skeleton graph creator

- **Sub-operation:** `stage1-skeleton-planner`
- **Model:** `gemini-3.1-pro-preview`
- **What it does:** Compresses the 3 process steps into a linear skeleton graph with short node descriptions.

### Response graph shape

```json
{
  "entryNodeId": "jd_input_screen",
  "endNodeId": "end",
  "nodes": [
    {
      "nodeId": "jd_input_screen",
      "type": "ui_screen",
      "name": "Job Description Input & Search Config",
      "stepId": "row_keetc3e3q",
      "description_chars": 1554
    },
    {
      "nodeId": "linkedin_sourcing_tool",
      "type": "tool",
      "name": "LinkedIn Sourcing & Scoring Tool",
      "stepId": "row_toc1mxpz8",
      "description_chars": 1603
    },
    {
      "nodeId": "candidate_profiles_screen",
      "type": "ui_screen",
      "name": "Candidate Profiles UI",
      "stepId": "row_o5h4okahg",
      "description_chars": 787
    },
    {
      "nodeId": "end",
      "type": "end",
      "name": "End",
      "stepId": null,
      "description_chars": 18
    }
  ],
  "edges": [
    {
      "fromNode": "jd_input_screen",
      "to": "linkedin_sourcing_tool",
      "kind": "sequential"
    },
    {
      "fromNode": "linkedin_sourcing_tool",
      "to": "candidate_profiles_screen",
      "kind": "sequential"
    },
    {
      "fromNode": "candidate_profiles_screen",
      "to": "end",
      "kind": "sequential"
    }
  ]
}
```


```
jd_input_screen  →  linkedin_sourcing_tool  →  candidate_profiles_screen  →  end
```

### Node descriptions after Stage 1 (compressed)

#### `jd_input_screen` (ui_screen) — 1554 chars

Fidelity: `{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`

```
Create a UI screen with a file upload/text area for 'New Job Description' (label='Upload/Paste JD') OR a dropdown to select an existing JD (label='Select Existing JD'). Add a number input for 'Candidate Count' (label='Number of Candidates', min=1, max=30). Submit button labelled 'Configure Search'. On submit, if a new JD is provided, call Gemini LLM to extract: job_title, job_category, company_name, department, location, work_mode, employment_type, seniority_level, min_years_experience (number), max_years_experience (number), management_experience_required (boolean), required_skills (list), preferred_skills (list), tools_and_technologies (list), education_level, certifications (list), key_responsibilities (list), domain_expertise (list), salary_min (number), salary_max (number), currency, benefits (list). Save to Collection 'job_description' schema: {job_title:string, job_category:string, company_name:string, department:string, location:string, work_mode:string, employment_type:string, seniority_level:string, min_years_experience:number, max_years_experience:number, management_experience_required:boolean, required_skills:list, preferred_skills:list, tools_and_technologies:list, education_level:string, certifications:list, key_responsibilities:list, domain_expertise:list, salary_min:number, salary_max:number, currency:string, benefits:list, jd_text:string}. If existing JD is selected, fetch these fields from 'job_description' by ID. Output: jd_id (string), candidate_count (number), and all the extracted JD scalar and list fields.
```


#### `linkedin_sourcing_tool` (tool) — 1603 chars

Fidelity: `{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`

```
Tool receives jd_id, candidate_count, and all extracted JD fields. 1) Call Gemini LLM to generate a LinkedIn search query returning keywords, title, and industry_hint. 2) Call Unipile API (api20.unipile.com:15002) to resolve location and industry_hint to LinkedIn filter IDs. 3) Call Unipile API paginated people search using resolved IDs and keywords until candidate_count is reached (max 30). 4) For each profile, call Unipile API in parallel (with 0.5-1s delay) to fetch sections: about, experience, education, skills, certifications. Retry throttled sections once after 2s. 5) Map fields and compute scores: Skills (30 pts based on matched_skills ratio), Experience (30 pts based on total_years_of_experience vs min_years_experience), Education (20 pts based on degree tier match), Open to Work (10 pts if actively_looking=='Yes'), Contact (5 pts email, 5 pts phone). 6) Upsert each profile into Collection 'all_Candidates' schema: {email:string, phone:string, first_name:string, last_name:string, location:string, total_years_of_experience:number, summary:string, skills:list, education:list, experience:list, source:string, actively_looking:string, current_base:string, status:string, linkedin_profile_url:string, linkedin_public_identifier:string, linkedin_provider_id:string, headline:string, profile_picture_url:string, network_distance:string, certifications:list, jd_id:string, score:number, score_breakdown:list, matched_skills:list} matching on linkedin_public_identifier. Set source='linkedin', status='linkedin_sourced'. Output: batch_id (string, set to jd_id) and fetched_count (number).
```


#### `candidate_profiles_screen` (ui_screen) — 787 chars

Fidelity: `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": false}`

```
UI screen to display fetched candidates. Backend handler reads batch_id and fetches profiles from Collection 'all_Candidates' where jd_id matches the batch_id, sorted by score descending. UI displays a list-level header showing fetched_count (e.g., 'Showing X profiles'), a sort toggle (score, experience, network distance), and a minimum score filter. Renders a profile card for each candidate showing: profile_picture_url, first_name, last_name, linkedin_profile_url, headline, location, score (with color badge: green>=70, orange 40-69, red<40), score_breakdown (visual bar), total_years_of_experience, actively_looking (badge if 'Yes'), matched_skills (chips), email, phone, and top 2-3 experience entries. Submit button labelled 'Finish Review'. Outputs: review_completed (boolean).
```


#### `end`

```
Ends the workflow.
```


### What changed vs original

| Node | Original chars | Stage 1 chars | Delta |
|---|---:|---:|---:|
| jd_input_screen | 3189 | 1554 | -1635 |
| linkedin_sourcing_tool | 7947 | 1603 | -6344 |
| candidate_profiles_screen | 829 | 787 | -42 |

Stage 1 kept Unipile host + a scoring outline for the tool, but **dropped** fidelity flags for `linkedin_sections` and detailed scoring weights (see tool fidelity in Stage 1 vs 1.2).

---

## 3. Stage 1.2 — Description enricher

- **Sub-operation:** `stage1_2-description-enricher`
- **Model:** `gemini-3.1-pro-preview`
- **What it does:** Re-expands each node description from the original process step + Stage 1 skeleton, restoring API paths, scoring rules, and UI layout detail. Graph topology (nodes/edges) is unchanged.

### Node descriptions after Stage 1.2 (restored / expanded)

#### `jd_input_screen` (ui_screen) — 3642 chars

Fidelity: `{"profile_cards_or_photo": false, "score_badge_or_breakdown": false, "matched_skills": false, "sort_or_filter": false, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": false, "candidate_count_max_30": true}`

Stage 1 → 1.2: **1554 → 3642** chars

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


#### `linkedin_sourcing_tool` (tool) — 4597 chars

Fidelity: `{"profile_cards_or_photo": false, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": true, "linkedin_sections": true, "scoring_weights": true, "all_candidates_collection": true, "candidate_count_max_30": true}`

Stage 1 → 1.2: **1603 → 4597** chars

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


#### `candidate_profiles_screen` (ui_screen) — 1840 chars

Fidelity: `{"profile_cards_or_photo": true, "score_badge_or_breakdown": true, "matched_skills": true, "sort_or_filter": true, "unipile_or_api20": false, "linkedin_sections": false, "scoring_weights": false, "all_candidates_collection": true, "candidate_count_max_30": true}`

Stage 1 → 1.2: **787 → 1840** chars

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


### Notable restores at Stage 1.2 (tool)

- `linkedin_sections=about,experience,education,skills,certifications` restored
- Unipile paths restored (`/linkedin/search/parameters`, `/linkedin/search/people`, `/api/v1/users/...`)
- Scoring weights restored (30/30/20/10/10)
- `limit=10`, throttle retry `2s`, `all_Candidates` upsert
- Still **not** byte-identical to original: e.g. no `https://` scheme on URLs; education aliases compressed (`Doctorate` / `MBA` / `BE/BS` omitted)

---

## 4. Stage 1.7 / Stage 2a — Schema builder (graph becomes typed)

### Stage 1.7

Descriptions are **unchanged** from Stage 1.2 (same char counts and fidelity). Checkpoint exists for continuity; no description rewrite.

```json
[
  {
    "nodeId": "jd_input_screen",
    "description_chars": 3642,
    "fidelity": {
      "profile_cards_or_photo": false,
      "score_badge_or_breakdown": false,
      "matched_skills": false,
      "sort_or_filter": false,
      "unipile_or_api20": false,
      "linkedin_sections": false,
      "scoring_weights": false,
      "all_candidates_collection": false,
      "candidate_count_max_30": true
    }
  },
  {
    "nodeId": "linkedin_sourcing_tool",
    "description_chars": 4597,
    "fidelity": {
      "profile_cards_or_photo": false,
      "score_badge_or_breakdown": true,
      "matched_skills": true,
      "sort_or_filter": true,
      "unipile_or_api20": true,
      "linkedin_sections": true,
      "scoring_weights": true,
      "all_candidates_collection": true,
      "candidate_count_max_30": true
    }
  },
  {
    "nodeId": "candidate_profiles_screen",
    "description_chars": 1840,
    "fidelity": {
      "profile_cards_or_photo": true,
      "score_badge_or_breakdown": true,
      "matched_skills": true,
      "sort_or_filter": true,
      "unipile_or_api20": false,
      "linkedin_sections": false,
      "scoring_weights": false,
      "all_candidates_collection": true,
      "candidate_count_max_30": true
    }
  }
]
```


### Stage 2a — Node schema builder

- **LLM calls:** two batches (`scratch_03` model=`gemini-2.5-pro`, `scratch_04` model=`gemini-2.5-pro`)
- **What it adds:** typed I/O (`nodeSchemas`), `edgeMap` / wiring, `stateSchema`, `availableVarsRegister`. **Node description text stays the Stage 1.2 text** (4597 / 3642 / 1840).

#### Descriptions still identical to Stage 1.2

- `jd_input_screen`: description unchanged vs 1.2 = **True** (3642 chars)
- `linkedin_sourcing_tool`: description unchanged vs 1.2 = **True** (4597 chars)
- `candidate_profiles_screen`: description unchanged vs 1.2 = **True** (1840 chars)

#### `nodeSchemas` produced at Stage 2a (full)

#### `jd_input_screen`

```json
{
  "nodeId": "jd_input_screen",
  "inputParams": [],
  "outputParams": [
    {
      "name": "jd_id",
      "datatype": "string",
      "required": true,
      "description": "The MongoDB ObjectId of the saved or selected Job Description.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "candidate_count",
      "datatype": "number",
      "required": true,
      "description": "The desired number of candidates to source (1-30).",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "job_title",
      "datatype": "string",
      "required": true,
      "description": "Official title or designation of the position.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "job_category",
      "datatype": "string",
      "required": true,
      "description": "Broad functional area or industry domain (e.g., Software Engineering).",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "company_name",
      "datatype": "string",
      "required": true,
      "description": "Name of the hiring company or organization.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "department",
      "datatype": "string",
      "required": true,
      "description": "Specific internal team, division, or business unit.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "location",
      "datatype": "string",
      "required": true,
      "description": "Primary physical location (city, state, country).",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "work_mode",
      "datatype": "string",
      "required": true,
      "description": "Work arrangement type (e.g., Remote, Hybrid, Onsite).",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "employment_type",
      "datatype": "string",
      "required": true,
      "description": "Engagement type (e.g., Full-Time, Part-Time, Contract).",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "seniority_level",
      "datatype": "string",
      "required": true,
      "description": "Career tier (e.g., Entry-Level, Senior, Manager).",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "min_years_experience",
      "datatype": "number",
      "required": true,
      "description": "Minimum required years of relevant experience.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "max_years_experience",
      "datatype": "number",
      "required": true,
      "description": "Maximum target years of experience.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "management_experience_required",
      "datatype": "boolean",
      "required": true,
      "description": "Whether people management or leadership experience is required.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "required_skills",
      "datatype": "list",
      "required": true,
      "description": "List of non-negotiable technical, functional, or soft skills.",
      "inputBindings": [],
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A required skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "preferred_skills",
      "datatype": "list",
      "required": true,
      "description": "List of nice-to-have or optional skills and qualifications.",
      "inputBindings": [],
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A preferred skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "tools_and_technologies",
      "datatype": "list",
      "required": true,
      "description": "List of software, platforms, tools, or programming languages required.",
      "inputBindings": [],
      "itemSchema": [
        {
          "name": "tool",
          "datatype": "string",
          "required": true,
          "description": "A required tool or technology.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "education_level",
      "datatype": "string",
      "required": true,
      "description": "Minimum academic qualification or degree needed.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "certifications",
      "datatype": "list",
      "required": true,
      "description": "List of required or preferred professional certifications and licenses.",
      "inputBindings": [],
      "itemSchema": [
        {
          "name": "certification",
          "datatype": "string",
          "required": true,
          "description": "A professional certification.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "key_responsibilities",
      "datatype": "list",
      "required": true,
      "description": "List of main duties, daily tasks, and role expectations.",
      "inputBindings": [],
      "itemSchema": [
        {
          "name": "responsibility",
          "datatype": "string",
          "required": true,
          "description": "A key responsibility.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "domain_expertise",
      "datatype": "list",
      "required": true,
      "description": "List of specific industry domains or technical practice areas.",
      "inputBindings": [],
      "itemSchema": [
        {
          "name": "domain",
          "datatype": "string",
          "required": true,
          "description": "A specific domain of expertise.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "salary_min",
      "datatype": "number",
      "required": true,
      "description": "Minimum base salary.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "salary_max",
      "datatype": "number",
      "required": true,
      "description": "Maximum base salary.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "currency",
      "datatype": "string",
      "required": true,
      "description": "3-letter currency code (e.g., USD, INR, EUR).",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "benefits",
      "datatype": "list",
      "required": true,
      "description": "List of perks, healthcare, allowances, and other work benefits.",
      "inputBindings": [],
      "itemSchema": [
        {
          "name": "benefit",
          "datatype": "string",
          "required": true,
          "description": "A work benefit.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    }
  ]
}
```

#### `linkedin_sourcing_tool`

```json
{
  "nodeId": "linkedin_sourcing_tool",
  "inputParams": [
    {
      "name": "jd_id",
      "datatype": "string",
      "required": true,
      "description": "The MongoDB ObjectId of the Job Description to source for.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.jd_id"
        }
      ],
      "itemSchema": null
    },
    {
      "name": "candidate_count",
      "datatype": "number",
      "required": true,
      "description": "The desired number of candidates to source.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.candidate_count"
        }
      ],
      "itemSchema": null
    },
    {
      "name": "job_title",
      "datatype": "string",
      "required": true,
      "description": "Official title or designation of the position.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.job_title"
        }
      ],
      "itemSchema": null
    },
    {
      "name": "location",
      "datatype": "string",
      "required": true,
      "description": "Primary physical location (city, state, country).",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.location"
        }
      ],
      "itemSchema": null
    },
    {
      "name": "min_years_experience",
      "datatype": "number",
      "required": true,
      "description": "Minimum required years of relevant experience.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.min_years_experience"
        }
      ],
      "itemSchema": null
    },
    {
      "name": "required_skills",
      "datatype": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.required_skills"
        }
      ],
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A required skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "preferred_skills",
      "datatype": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.preferred_skills"
        }
      ],
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A preferred skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "tools_and_technologies",
      "datatype": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.tools_and_technologies"
        }
      ],
      "itemSchema": [
        {
          "name": "tool",
          "datatype": "string",
          "required": true,
          "description": "A required tool or technology.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "education_level",
      "datatype": "string",
      "required": true,
      "description": "Minimum academic qualification, used for scoring.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.education_level"
        }
      ],
      "itemSchema": null
    }
  ],
  "outputParams": [
    {
      "name": "batch_id",
      "datatype": "string",
      "required": true,
      "description": "Identifier for the sourcing batch, same as the input jd_id.",
      "inputBindings": [],
      "itemSchema": null
    },
    {
      "name": "fetched_count",
      "datatype": "number",
      "required": true,
      "description": "The actual number of candidate profiles fetched and saved.",
      "inputBindings": [],
      "itemSchema": null
    }
  ]
}
```

#### `candidate_profiles_screen`

```json
{
  "nodeId": "candidate_profiles_screen",
  "inputParams": [
    {
      "name": "batch_id",
      "datatype": "string",
      "required": true,
      "description": "Identifier for the sourcing batch, used to query candidates from the database.",
      "inputBindings": [
        {
          "boundTo": "linkedin_sourcing_tool.batch_id"
        }
      ],
      "itemSchema": null
    },
    {
      "name": "fetched_count",
      "datatype": "number",
      "required": true,
      "description": "The actual number of candidate profiles fetched, for display.",
      "inputBindings": [
        {
          "boundTo": "linkedin_sourcing_tool.fetched_count"
        }
      ],
      "itemSchema": null
    },
    {
      "name": "candidate_count",
      "datatype": "number",
      "required": true,
      "description": "The number of candidates originally requested, for display.",
      "inputBindings": [
        {
          "boundTo": "jd_input_screen.candidate_count"
        }
      ],
      "itemSchema": null
    }
  ],
  "outputParams": [
    {
      "name": "review_completed",
      "datatype": "boolean",
      "required": true,
      "description": "Indicates the user has finished reviewing candidates. Always true on submit.",
      "inputBindings": [],
      "itemSchema": null
    }
  ]
}
```


#### `edgeMap`

```json
null
```


#### `stateSchema`

```json
null
```


#### `availableVarsRegister`

```json
{
  "globals": [],
  "secondaryGlobals": [],
  "nodeOutputs": [
    {
      "nodeId": "jd_input_screen",
      "batch": 0,
      "outputs": [
        {
          "name": "jd_id",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "candidate_count",
          "datatype": "number",
          "itemSchema": null
        },
        {
          "name": "job_title",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "job_category",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "company_name",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "department",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "location",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "work_mode",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "employment_type",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "seniority_level",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "min_years_experience",
          "datatype": "number",
          "itemSchema": null
        },
        {
          "name": "max_years_experience",
          "datatype": "number",
          "itemSchema": null
        },
        {
          "name": "management_experience_required",
          "datatype": "boolean",
          "itemSchema": null
        },
        {
          "name": "required_skills",
          "datatype": "list",
          "itemSchema": [
            {
              "name": "skill",
              "datatype": "string",
              "required": true,
              "description": "A required skill.",
              "inputBindings": [],
              "itemSchema": null
            }
          ]
        },
        {
          "name": "preferred_skills",
          "datatype": "list",
          "itemSchema": [
            {
              "name": "skill",
              "datatype": "string",
              "required": true,
              "description": "A preferred skill.",
              "inputBindings": [],
              "itemSchema": null
            }
          ]
        },
        {
          "name": "tools_and_technologies",
          "datatype": "list",
          "itemSchema": [
            {
              "name": "tool",
              "datatype": "string",
              "required": true,
              "description": "A required tool or technology.",
              "inputBindings": [],
              "itemSchema": null
            }
          ]
        },
        {
          "name": "education_level",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "certifications",
          "datatype": "list",
          "itemSchema": [
            {
              "name": "certification",
              "datatype": "string",
              "required": true,
              "description": "A professional certification.",
              "inputBindings": [],
              "itemSchema": null
            }
          ]
        },
        {
          "name": "key_responsibilities",
          "datatype": "list",
          "itemSchema": [
            {
              "name": "responsibility",
              "datatype": "string",
              "required": true,
              "description": "A key responsibility.",
              "inputBindings": [],
              "itemSchema": null
            }
          ]
        },
        {
          "name": "domain_expertise",
          "datatype": "list",
          "itemSchema": [
            {
              "name": "domain",
              "datatype": "string",
              "required": true,
              "description": "A specific domain of expertise.",
              "inputBindings": [],
              "itemSchema": null
            }
          ]
        },
        {
          "name": "salary_min",
          "datatype": "number",
          "itemSchema": null
        },
        {
          "name": "salary_max",
          "datatype": "number",
          "itemSchema": null
        },
        {
          "name": "currency",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "benefits",
          "datatype": "list",
          "itemSchema": [
            {
              "name": "benefit",
              "datatype": "string",
              "required": true,
              "description": "A work benefit.",
              "inputBindings": [],
              "itemSchema": null
            }
          ]
        }
      ]
    },
    {
      "nodeId": "linkedin_sourcing_tool",
      "batch": 0,
      "outputs": [
        {
          "name": "batch_id",
          "datatype": "string",
          "itemSchema": null
        },
        {
          "name": "fetched_count",
          "datatype": "number",
          "itemSchema": null
        }
      ]
    },
    {
      "nodeId": "candidate_profiles_screen",
      "batch": 0,
      "outputs": [
        {
          "name": "review_completed",
          "datatype": "boolean",
          "itemSchema": null
        }
      ]
    }
  ]
}
```


#### Raw Stage 2a LLM parsed payloads

<details><summary>scratch_03_stage2a-node-schema-builder_response.json (parsed)</summary>

```json
null
```


</details>

<details><summary>scratch_04_stage2a-node-schema-builder_response.json (parsed)</summary>

```json
null
```


</details>

---

## 5. Stage 4c — Persistence + intent freeze

- **Sub-operation:** `stage4c-persistence-extract` / model=`gemini-3.5-flash-lite`
- **What it does:** persistence extraction; freezes the Stage 1.2 description into both `generationDescription` and immutable **`intentDescription`**.

### Descriptions at Stage 4c (still Stage 1.2 text)

- `jd_input_screen`: 3642 chars; identical to Stage 1.2 = **True**
- `linkedin_sourcing_tool`: 4597 chars; identical to Stage 1.2 = **True**
- `candidate_profiles_screen`: 1840 chars; identical to Stage 1.2 = **True**

### Final graph wiring + intent freeze (`final_graph.json`)

### `jd_input_screen` (`ui_screen`)

- `intentDescription` chars: 3642
- `generationDescription` chars: 3642
- intent == generationDescription: True

**declaredOutputs / inputBindings (graph wiring):**

```json
{
  "inputBindings": {},
  "declaredOutputs": [
    {
      "name": "jd_id",
      "dataType": "string",
      "required": true,
      "description": "The MongoDB ObjectId of the saved or selected Job Description.",
      "itemSchema": null
    },
    {
      "name": "candidate_count",
      "dataType": "number",
      "required": true,
      "description": "The desired number of candidates to source (1-30).",
      "itemSchema": null
    },
    {
      "name": "job_title",
      "dataType": "string",
      "required": true,
      "description": "Official title or designation of the position.",
      "itemSchema": null
    },
    {
      "name": "job_category",
      "dataType": "string",
      "required": true,
      "description": "Broad functional area or industry domain (e.g., Software Engineering).",
      "itemSchema": null
    },
    {
      "name": "company_name",
      "dataType": "string",
      "required": true,
      "description": "Name of the hiring company or organization.",
      "itemSchema": null
    },
    {
      "name": "department",
      "dataType": "string",
      "required": true,
      "description": "Specific internal team, division, or business unit.",
      "itemSchema": null
    },
    {
      "name": "location",
      "dataType": "string",
      "required": true,
      "description": "Primary physical location (city, state, country).",
      "itemSchema": null
    },
    {
      "name": "work_mode",
      "dataType": "string",
      "required": true,
      "description": "Work arrangement type (e.g., Remote, Hybrid, Onsite).",
      "itemSchema": null
    },
    {
      "name": "employment_type",
      "dataType": "string",
      "required": true,
      "description": "Engagement type (e.g., Full-Time, Part-Time, Contract).",
      "itemSchema": null
    },
    {
      "name": "seniority_level",
      "dataType": "string",
      "required": true,
      "description": "Career tier (e.g., Entry-Level, Senior, Manager).",
      "itemSchema": null
    },
    {
      "name": "min_years_experience",
      "dataType": "number",
      "required": true,
      "description": "Minimum required years of relevant experience.",
      "itemSchema": null
    },
    {
      "name": "max_years_experience",
      "dataType": "number",
      "required": true,
      "description": "Maximum target years of experience.",
      "itemSchema": null
    },
    {
      "name": "management_experience_required",
      "dataType": "boolean",
      "required": true,
      "description": "Whether people management or leadership experience is required.",
      "itemSchema": null
    },
    {
      "name": "required_skills",
      "dataType": "list",
      "required": true,
      "description": "List of non-negotiable technical, functional, or soft skills.",
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A required skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "preferred_skills",
      "dataType": "list",
      "required": true,
      "description": "List of nice-to-have or optional skills and qualifications.",
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A preferred skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "tools_and_technologies",
      "dataType": "list",
      "required": true,
      "description": "List of software, platforms, tools, or programming languages required.",
      "itemSchema": [
        {
          "name": "tool",
          "datatype": "string",
          "required": true,
          "description": "A required tool or technology.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "education_level",
      "dataType": "string",
      "required": true,
      "description": "Minimum academic qualification or degree needed.",
      "itemSchema": null
    },
    {
      "name": "certifications",
      "dataType": "list",
      "required": true,
      "description": "List of required or preferred professional certifications and licenses.",
      "itemSchema": [
        {
          "name": "certification",
          "datatype": "string",
          "required": true,
          "description": "A professional certification.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "key_responsibilities",
      "dataType": "list",
      "required": true,
      "description": "List of main duties, daily tasks, and role expectations.",
      "itemSchema": [
        {
          "name": "responsibility",
          "datatype": "string",
          "required": true,
          "description": "A key responsibility.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "domain_expertise",
      "dataType": "list",
      "required": true,
      "description": "List of specific industry domains or technical practice areas.",
      "itemSchema": [
        {
          "name": "domain",
          "datatype": "string",
          "required": true,
          "description": "A specific domain of expertise.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "salary_min",
      "dataType": "number",
      "required": true,
      "description": "Minimum base salary.",
      "itemSchema": null
    },
    {
      "name": "salary_max",
      "dataType": "number",
      "required": true,
      "description": "Maximum base salary.",
      "itemSchema": null
    },
    {
      "name": "currency",
      "dataType": "string",
      "required": true,
      "description": "3-letter currency code (e.g., USD, INR, EUR).",
      "itemSchema": null
    },
    {
      "name": "benefits",
      "dataType": "list",
      "required": true,
      "description": "List of perks, healthcare, allowances, and other work benefits.",
      "itemSchema": [
        {
          "name": "benefit",
          "datatype": "string",
          "required": true,
          "description": "A work benefit.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    }
  ],
  "stateReads": [],
  "stateWrites": [
    "jd_input_screen__jd_id",
    "jd_input_screen__candidate_count",
    "jd_input_screen__job_title",
    "jd_input_screen__job_category",
    "jd_input_screen__company_name",
    "jd_input_screen__department",
    "jd_input_screen__location",
    "jd_input_screen__work_mode",
    "jd_input_screen__employment_type",
    "jd_input_screen__seniority_level",
    "jd_input_screen__min_years_experience",
    "jd_input_screen__max_years_experience",
    "jd_input_screen__management_experience_required",
    "jd_input_screen__required_skills",
    "jd_input_screen__preferred_skills",
    "jd_input_screen__tools_and_technologies",
    "jd_input_screen__education_level",
    "jd_input_screen__certifications",
    "jd_input_screen__key_responsibilities",
    "jd_input_screen__domain_expertise",
    "jd_input_screen__salary_min",
    "jd_input_screen__salary_max",
    "jd_input_screen__currency",
    "jd_input_screen__benefits"
  ]
}
```


**generation I/O (after gap paths):**

```json
{
  "inputs": [],
  "outputs": [
    {
      "name": "jd_id",
      "dataType": "string",
      "required": true,
      "description": "The MongoDB ObjectId of the saved or selected Job Description.",
      "itemSchema": null
    },
    {
      "name": "candidate_count",
      "dataType": "number",
      "required": true,
      "description": "The desired number of candidates to source (1-30).",
      "itemSchema": null
    },
    {
      "name": "job_title",
      "dataType": "string",
      "required": true,
      "description": "Official title or designation of the position.",
      "itemSchema": null
    },
    {
      "name": "job_category",
      "dataType": "string",
      "required": true,
      "description": "Broad functional area or industry domain (e.g., Software Engineering).",
      "itemSchema": null
    },
    {
      "name": "company_name",
      "dataType": "string",
      "required": true,
      "description": "Name of the hiring company or organization.",
      "itemSchema": null
    },
    {
      "name": "department",
      "dataType": "string",
      "required": true,
      "description": "Specific internal team, division, or business unit.",
      "itemSchema": null
    },
    {
      "name": "location",
      "dataType": "string",
      "required": true,
      "description": "Primary physical location (city, state, country).",
      "itemSchema": null
    },
    {
      "name": "work_mode",
      "dataType": "string",
      "required": true,
      "description": "Work arrangement type (e.g., Remote, Hybrid, Onsite).",
      "itemSchema": null
    },
    {
      "name": "employment_type",
      "dataType": "string",
      "required": true,
      "description": "Engagement type (e.g., Full-Time, Part-Time, Contract).",
      "itemSchema": null
    },
    {
      "name": "seniority_level",
      "dataType": "string",
      "required": true,
      "description": "Career tier (e.g., Entry-Level, Senior, Manager).",
      "itemSchema": null
    },
    {
      "name": "min_years_experience",
      "dataType": "number",
      "required": true,
      "description": "Minimum required years of relevant experience.",
      "itemSchema": null
    },
    {
      "name": "max_years_experience",
      "dataType": "number",
      "required": true,
      "description": "Maximum target years of experience.",
      "itemSchema": null
    },
    {
      "name": "management_experience_required",
      "dataType": "boolean",
      "required": true,
      "description": "Whether people management or leadership experience is required.",
      "itemSchema": null
    },
    {
      "name": "required_skills",
      "dataType": "list",
      "required": true,
      "description": "List of non-negotiable technical, functional, or soft skills.",
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A required skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "preferred_skills",
      "dataType": "list",
      "required": true,
      "description": "List of nice-to-have or optional skills and qualifications.",
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A preferred skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "tools_and_technologies",
      "dataType": "list",
      "required": true,
      "description": "List of software, platforms, tools, or programming languages required.",
      "itemSchema": [
        {
          "name": "tool",
          "datatype": "string",
          "required": true,
          "description": "A required tool or technology.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "education_level",
      "dataType": "string",
      "required": true,
      "description": "Minimum academic qualification or degree needed.",
      "itemSchema": null
    },
    {
      "name": "certifications",
      "dataType": "list",
      "required": true,
      "description": "List of required or preferred professional certifications and licenses.",
      "itemSchema": [
        {
          "name": "certification",
          "datatype": "string",
          "required": true,
          "description": "A professional certification.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "key_responsibilities",
      "dataType": "list",
      "required": true,
      "description": "List of main duties, daily tasks, and role expectations.",
      "itemSchema": [
        {
          "name": "responsibility",
          "datatype": "string",
          "required": true,
          "description": "A key responsibility.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "domain_expertise",
      "dataType": "list",
      "required": true,
      "description": "List of specific industry domains or technical practice areas.",
      "itemSchema": [
        {
          "name": "domain",
          "datatype": "string",
          "required": true,
          "description": "A specific domain of expertise.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "salary_min",
      "dataType": "number",
      "required": true,
      "description": "Minimum base salary.",
      "itemSchema": null
    },
    {
      "name": "salary_max",
      "dataType": "number",
      "required": true,
      "description": "Maximum base salary.",
      "itemSchema": null
    },
    {
      "name": "currency",
      "dataType": "string",
      "required": true,
      "description": "3-letter currency code (e.g., USD, INR, EUR).",
      "itemSchema": null
    },
    {
      "name": "benefits",
      "dataType": "list",
      "required": true,
      "description": "List of perks, healthcare, allowances, and other work benefits.",
      "itemSchema": [
        {
          "name": "benefit",
          "datatype": "string",
          "required": true,
          "description": "A work benefit.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    }
  ],
  "function_code_chars": 0,
  "has_intermediateRepresentation": false
}
```


### `linkedin_sourcing_tool` (`tool`)

- `intentDescription` chars: 4597
- `generationDescription` chars: 4597
- intent == generationDescription: True

**declaredOutputs / inputBindings (graph wiring):**

```json
{
  "inputBindings": {
    "jd_id": "{{jd_input_screen.jd_id}}",
    "candidate_count": "{{jd_input_screen.candidate_count}}",
    "job_title": "{{jd_input_screen.job_title}}",
    "location": "{{jd_input_screen.location}}",
    "min_years_experience": "{{jd_input_screen.min_years_experience}}",
    "required_skills": "{{jd_input_screen.required_skills}}",
    "preferred_skills": "{{jd_input_screen.preferred_skills}}",
    "tools_and_technologies": "{{jd_input_screen.tools_and_technologies}}",
    "education_level": "{{jd_input_screen.education_level}}"
  },
  "declaredOutputs": [
    {
      "name": "batch_id",
      "dataType": "string",
      "required": true,
      "description": "Identifier for the sourcing batch, same as the input jd_id.",
      "itemSchema": null
    },
    {
      "name": "fetched_count",
      "dataType": "number",
      "required": true,
      "description": "The actual number of candidate profiles fetched and saved.",
      "itemSchema": null
    }
  ],
  "stateReads": [
    "jd_input_screen__jd_id",
    "jd_input_screen__candidate_count",
    "jd_input_screen__job_title",
    "jd_input_screen__location",
    "jd_input_screen__min_years_experience",
    "jd_input_screen__required_skills",
    "jd_input_screen__preferred_skills",
    "jd_input_screen__tools_and_technologies",
    "jd_input_screen__education_level"
  ],
  "stateWrites": [
    "linkedin_sourcing_tool__batch_id",
    "linkedin_sourcing_tool__fetched_count"
  ]
}
```


**generation I/O (after gap paths):**

```json
{
  "inputs": [
    {
      "name": "jd_id",
      "dataType": "string",
      "required": true,
      "description": "The MongoDB ObjectId of the Job Description to source for.",
      "itemSchema": null
    },
    {
      "name": "candidate_count",
      "dataType": "number",
      "required": true,
      "description": "The desired number of candidates to source.",
      "itemSchema": null
    },
    {
      "name": "job_title",
      "dataType": "string",
      "required": true,
      "description": "Official title or designation of the position.",
      "itemSchema": null
    },
    {
      "name": "location",
      "dataType": "string",
      "required": true,
      "description": "Primary physical location (city, state, country).",
      "itemSchema": null
    },
    {
      "name": "min_years_experience",
      "dataType": "number",
      "required": true,
      "description": "Minimum required years of relevant experience.",
      "itemSchema": null
    },
    {
      "name": "required_skills",
      "dataType": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills.",
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A required skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "preferred_skills",
      "dataType": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills.",
      "itemSchema": [
        {
          "name": "skill",
          "datatype": "string",
          "required": true,
          "description": "A preferred skill.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "tools_and_technologies",
      "dataType": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills.",
      "itemSchema": [
        {
          "name": "tool",
          "datatype": "string",
          "required": true,
          "description": "A required tool or technology.",
          "inputBindings": [],
          "itemSchema": null
        }
      ]
    },
    {
      "name": "education_level",
      "dataType": "string",
      "required": true,
      "description": "Minimum academic qualification, used for scoring.",
      "itemSchema": null
    }
  ],
  "outputs": [
    {
      "name": "batch_id",
      "dataType": "string",
      "required": true,
      "description": "Identifier for the sourcing batch, same as the input jd_id.",
      "itemSchema": null
    },
    {
      "name": "fetched_count",
      "dataType": "number",
      "required": true,
      "description": "The actual number of candidate profiles fetched and saved.",
      "itemSchema": null
    }
  ],
  "function_code_chars": 0,
  "has_intermediateRepresentation": false
}
```


### `candidate_profiles_screen` (`ui_screen`)

- `intentDescription` chars: 1840
- `generationDescription` chars: 1840
- intent == generationDescription: True

**declaredOutputs / inputBindings (graph wiring):**

```json
{
  "inputBindings": {
    "batch_id": "{{linkedin_sourcing_tool.batch_id}}",
    "fetched_count": "{{linkedin_sourcing_tool.fetched_count}}",
    "candidate_count": "{{jd_input_screen.candidate_count}}"
  },
  "declaredOutputs": [
    {
      "name": "review_completed",
      "dataType": "boolean",
      "required": true,
      "description": "Indicates the user has finished reviewing candidates. Always true on submit.",
      "itemSchema": null
    }
  ],
  "stateReads": [
    "linkedin_sourcing_tool__batch_id",
    "linkedin_sourcing_tool__fetched_count",
    "jd_input_screen__candidate_count"
  ],
  "stateWrites": [
    "candidate_profiles_screen__review_completed"
  ]
}
```


**generation I/O (after gap paths):**

```json
{
  "inputs": [
    {
      "name": "batch_id",
      "dataType": "string",
      "required": true,
      "description": "Identifier for the sourcing batch, used to query candidates from the database.",
      "itemSchema": null
    },
    {
      "name": "fetched_count",
      "dataType": "number",
      "required": true,
      "description": "The actual number of candidate profiles fetched, for display.",
      "itemSchema": null
    },
    {
      "name": "candidate_count",
      "dataType": "number",
      "required": true,
      "description": "The number of candidates originally requested, for display.",
      "itemSchema": null
    }
  ],
  "outputs": [
    {
      "name": "review_completed",
      "dataType": "boolean",
      "required": true,
      "description": "Indicates the user has finished reviewing candidates. Always true on submit.",
      "itemSchema": null
    }
  ],
  "function_code_chars": 0,
  "has_intermediateRepresentation": false
}
```


### `end` (`end`)

- `intentDescription` chars: 0
- `generationDescription` chars: 0
- intent == generationDescription: True

**declaredOutputs / inputBindings (graph wiring):**

```json
{
  "inputBindings": {},
  "declaredOutputs": [],
  "stateReads": [],
  "stateWrites": []
}
```



<details><summary>Stage 4c LLM parsed response</summary>

```json
{
  "extractions": [
    {
      "nodeId": "jd_input_screen",
      "operations": [
        {
          "kind": "read",
          "collection": "job_description",
          "fields": [],
          "filter_keys": [
            "_id"
          ],
          "relationships": [],
          "evidence": "Dropdown populated by querying the 'job_description' collection (display 'job_title' + 'company_name', value '_id')."
        },
        {
          "kind": "write",
          "collection": "job_description",
          "fields": [
            {
              "name": "job_title",
              "type": "string",
              "evidence": "job_title: Official title or designation of the position"
            },
            {
              "name": "job_category",
              "type": "string",
              "evidence": "job_category: Broad functional area or industry domain"
            },
            {
              "name": "company_name",
              "type": "string",
              "evidence": "company_name: Name of the hiring company or organization"
            },
            {
              "name": "department",
              "type": "string",
              "evidence": "department: Specific internal team, division, or business unit"
            },
            {
              "name": "location",
              "type": "string",
              "evidence": "location: Primary physical location"
            },
            {
              "name": "work_mode",
              "type": "string",
              "evidence": "work_mode: Work arrangement type"
            },
            {
              "name": "employment_type",
              "type": "string",
              "evidence": "employment_type: Engagement type"
            },
            {
              "name": "seniority_level",
              "type": "string",
              "evidence": "seniority_level: Career tier"
            },
            {
              "name": "min_years_experience",
              "type": "number",
              "evidence": "min_years_experience: Minimum required years of relevant experience"
            },
            {
              "name": "max_years_experience",
              "type": "number",
              "evidence": "max_years_experience: Maximum target years of experience"
            },
            {
              "name": "management_experience_required",
              "type": "boolean",
              "evidence": "management_experience_required: Boolean"
            },
            {
              "name": "required_skills",
              "type": "array",
              "evidence": "required_skills: Array of non-negotiable technical, functional, or soft skills"
            },
            {
              "name": "preferred_skills",
              "type": "array",
              "evidence": "preferred_skills: Array of nice-to-have or optional skills"
            },
            {
              "name": "tools_and_technologies",
              "type": "array",
              "evidence": "tools_and_technologies: Array of software, platforms, tools, or programming languages"
            },
            {
              "name": "education_level",
              "type": "string",
              "evidence": "education_level: Minimum academic qualification or degree needed"
            },
            {
              "name": "certifications",
              "type": "array",
              "evidence": "certifications: Array of required or preferred professional certifications"
            },
            {
              "name": "key_responsibilities",
              "type": "array",
              "evidence": "key_responsibilities: Array of main duties, daily tasks"
            },
            {
              "name": "domain_expertise",
              "type": "array",
              "evidence": "domain_expertise: Array of specific industry domains"
            },
            {
              "name": "salary_min",
              "type": "number",
              "evidence": "salary_min: Minimum base salary"
            },
            {
              "name": "salary_max",
              "type": "number",
              "evidence": "salary_max: Maximum base salary"
            },
            {
              "name": "currency",
              "type": "string",
              "evidence": "currency: 3-letter currency code"
            },
            {
              "name": "benefits",
              "type": "array",
              "evidence": "benefits: Array of perks, healthcare, allowances"
            }
          ],
          "filter_keys": [],
          "relationships": [],
          "evidence": "Insert into 'job_description' collection: { ...extracted_json, jd_text: \"<original_text>\", created_at: <UTC_timestamp> }."
        }
      ]
    },
    {
      "nodeId": "linkedin_sourcing_tool",
      "operations": [
        {
          "kind": "write",
          "collection": "all_Candidates",
          "fields": [
            {
              "name": "file_name",
              "type": "null",
              "evidence": "file_name, resume_file_hash, resume_file_link... = null"
            },
            {
              "name": "resume_file_hash",
              "type": "null",
              "evidence": "resume_file_hash = null"
            },
            {
              "name": "resume_file_link",
              "type": "null",
              "evidence": "resume_file_link = null"
            },
            {
              "name": "processed_at",
              "type": "string",
              "evidence": "processed_at = UTC now"
            },
            {
              "name": "email",
              "type": "string",
              "evidence": "email = contact_info.emails[0] (or null)"
            },
            {
              "name": "phone",
              "type": "string",
              "evidence": "phone = contact_info.phones[0] (or null)"
            },
            {
              "name": "first_name",
              "type": "string",
              "evidence": "first_name = first_name"
            },
            {
              "name": "last_name",
              "type": "string",
              "evidence": "last_name = last_name"
            },
            {
              "name": "location",
              "type": "string",
              "evidence": "location = location"
            },
            {
              "name": "total_years_of_experience",
              "type": "number",
              "evidence": "total_years_of_experience = sum of experience durations"
            },
            {
              "name": "summary",
              "type": "string",
              "evidence": "summary = about"
            },
            {
              "name": "skills",
              "type": "array",
              "evidence": "skills = skills[].name"
            },
            {
              "name": "education",
              "type": "array",
              "evidence": "education = education[] mapped to {degree, institution, year}"
            },
            {
              "name": "experience",
              "type": "array",
              "evidence": "experience = work_experience[] mapped to {title, company, start_date, end_date, description}"
            },
            {
              "name": "source",
              "type": "string",
              "evidence": "source = \"linkedin\""
            },
            {
              "name": "status",
              "type": "string",
              "evidence": "status = \"linkedin_sourced\""
            },
            {
              "name": "actively_looking",
              "type": "string",
              "evidence": "actively_looking = \"Yes\" if is_open_to_work == true else \"No\""
            },
            {
              "name": "jd_id",
              "type": "string",
              "evidence": "jd_id = jd_id"
            }
          ],
          "filter_keys": [
            "linkedin_public_identifier"
          ],
          "relationships": [
            {
              "from_field": "jd_id",
              "to_collection": "job_description",
              "to_field": "_id"
            }
          ],
          "evidence": "Upsert each profile into 'all_Candidates' collection matching on 'linkedin_public_identifier'."
        }
      ]
    },
    {
      "nodeId": "candidate_profiles_screen",
      "operations": [
        {
          "kind": "read",
          "collection": "all_Candidates",
          "fields": [],
          "filter_keys": [
            "jd_id"
          ],
          "relationships": [
            {
              "from_field": "jd_id",
              "to_collection": "job_description",
              "to_field": "_id"
            }
          ],
          "evidence": "Backend handler queries the 'all_Candidates' collection where `jd_id` == `batch_id`"
        }
      ]
    }
  ]
}
```


</details>

---

## 6. Gap tool creator

**Node:** `linkedin_sourcing_tool`

- Initial import error (later continued with mocks): `No module named 'crewai'`
- Intent fidelity in prompt (from `tool_result_summary.json`):

```json
{
  "unipile_or_api20": true,
  "linkedin_sections": true,
  "scoring_weights": true,
  "all_candidates_collection": true,
  "matched_skills": true
}
```


### Prompt sent to gap tool LLM (full)

_File:_ `tool_gap_prompt.txt` (24956 chars)

```
Create a Python LangChain-compatible CUSTOM TOOL definition as JSON.
The tool's registry name (no spaces) MUST be EXACTLY:
  linkedin_sourcing_trace_tool
Use that exact string for the JSON "name" field.

Task / node description:
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

WORKFLOW IO CONTRACT — your tool MUST satisfy these parameter contracts exactly:
{
  "inputParameters": [
    {
      "name": "jd_id",
      "dataType": "string",
      "required": true,
      "description": "The MongoDB ObjectId of the Job Description to source for."
    },
    {
      "name": "candidate_count",
      "dataType": "number",
      "required": true,
      "description": "The desired number of candidates to source."
    },
    {
      "name": "job_title",
      "dataType": "string",
      "required": true,
      "description": "Official title or designation of the position."
    },
    {
      "name": "location",
      "dataType": "string",
      "required": true,
      "description": "Primary physical location (city, state, country)."
    },
    {
      "name": "min_years_experience",
      "dataType": "number",
      "required": true,
      "description": "Minimum required years of relevant experience."
    },
    {
      "name": "required_skills",
      "dataType": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills."
    },
    {
      "name": "preferred_skills",
      "dataType": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills."
    },
    {
      "name": "tools_and_technologies",
      "dataType": "list",
      "required": true,
      "description": "Used to generate search queries and for scoring candidate skills."
    },
    {
      "name": "education_level",
      "dataType": "string",
      "required": true,
      "description": "Minimum academic qualification, used for scoring."
    }
  ],
  "outputParameters": [
    {
      "name": "batch_id",
      "dataType": "string",
      "required": true,
      "description": "Identifier for the sourcing batch, same as the input jd_id."
    },
    {
      "name": "fetched_count",
      "dataType": "number",
      "required": true,
      "description": "The actual number of candidate profiles fetched and saved."
    }
  ]
}


AVAILABLE PROJECT CREDENTIALS (use the exact 'name' when reading from request_meta):
[
  {
    "name": "UNIPILE_API_KEY",
    "type": "api_key",
    "service": "Unipile"
  },
  {
    "name": "gemini_api_key",
    "type": "api_key",
    "service": "Gemini"
  }
]

To use a credential in function_code:
  creds = (request_meta or {}).get("credentials") or {}
  api_key = creds.get("exact_credential_name_from_list_above")
  if not api_key:
      return {"error": "Missing credential 'exact_credential_name'. Ask admin to configure it."}



MONGODB IS ALREADY AVAILABLE IN THIS BACKEND (do NOT return capability_gap for missing URI/DB):
- Dynamic tools implement LangChain `BaseTool._run`, which is SYNCHRONOUS.
  Therefore: DO NOT use `await` anywhere in function_code.
- Use `Database.get_sync_collection("<collection_name>")` to access collections.
- Put `from services.database import Database` in the JSON "imports" array (NOT inside function_code).
- Database connection details (URI + DB name) are configured by the platform (env/config.json) and are NOT
  supposed to be read directly inside tool code.
- IMPORTANT: You MAY import and use Database even though it reads env internally; that is the supported pattern.

MongoDB example patterns (adapt to the IO contract; do not invent fields):
1) Insert:
  coll = Database.get_sync_collection("users")
  doc = {"username": username, "email": email, "password_hash": password_hash}
  res = coll.insert_one(doc)
  return {"inserted_id": str(res.inserted_id)}

2) Upsert:
  coll = Database.get_sync_collection("ekyc_details")
  filters = {"user_id": user_id}
  updates = {"aadhaar_last4": aadhaar_last4, "kyc_payload": kyc_payload, "verified": True}
  res = coll.update_one(filters, {"$set": updates}, upsert=True)
  upserted = getattr(res, "upserted_id", None)
  return {"matched": int(res.matched_count), "modified": int(res.modified_count), "upserted_id": str(upserted) if upserted else None}

IMPLEMENTATION STRATEGY (derive from task + io_contract + credentials — do NOT hardcode vendors):
- Implement exactly what the task description and io_contract require; no unrelated logic.
- Secrets/API keys: ONLY via request_meta['credentials'] using exact names from AVAILABLE PROJECT CREDENTIALS.
- Hosted LLM / vision / chat: pick the SDK or REST client that matches a configured credential
  (Gemini → google.generativeai, OpenAI → openai, Claude → anthropic, etc.).
  Use model id strings from the MODEL RESEARCH section (from Google Search) — never guess legacy defaults.
- External HTTP microservices: requests or httpx with base URL/auth from credentials or explicit io_contract fields.
- OCR / image text extraction when no suitable LLM/vision credential exists: fall back to pytesseract + PIL (installed).
- File/binary inputs: decode or read real bytes (e.g. base64.b64decode); never pretend to process opaque IDs without a resolver.
- NEVER return capability_gap / stub code because a credential is missing from AVAILABLE PROJECT CREDENTIALS.
  Always write the real implementation. At runtime, if creds.get('NAME') is missing, return
  {"error": "Missing credential 'NAME'. Ask admin to configure it."} (and fill all outputParameters keys).
- If the task has OPTIONAL integration paths (e.g. PDF upload OR email OR shared folder), implement ONLY
  the path(s) described in the task / io_contract — do not require credentials for unused options.
- Always return a dict whose keys match outputParameters; use clear error or status fields on failure paths.
- Configured credential names (use these exact keys only): UNIPILE_API_KEY, gemini_api_key



MODEL RESEARCH (Google Search — authoritative for model id strings in function_code):
(Research unavailable.) Pick model ids from each provider's current official API docs for the capabilities this task needs (vision, chat, etc.). Do not use ids known to be deprecated (gemini-2.5-*, gemini-pro-vision, gpt-3.5-turbo, claude-2*, etc.).

Use the model id strings from this research in your generated function_code. Do not guess outdated defaults.



IMPORTANT - Library Usage Strategy (pick what the TASK needs — examples are not prescriptions):
    PREFER stdlib when sufficient: json, re, hashlib, datetime, base64, difflib, etc.
    COMMON INSTALLED PACKAGES (use only if required by your chosen approach):
    - HTTP: requests, httpx
    - Images/OCR fallback: PIL (Pillow), pytesseract
    - LLM SDKs (match configured credential): google.generativeai, openai, anthropic
    - Documents: PyPDF2, pdfplumber, python-docx
    - Text similarity: fuzzywuzzy (or stdlib difflib)
    - Payments/other: stripe, pandas, numpy — only when the task truly needs them

    BEFORE adding an import: confirm it is required for this specific task and credential set.
    

COMMON PITFALLS THAT CAUSE AUTOMATIC REJECTION (avoid these):
- Putting import statements inside function_code — imports belong ONLY in the imports array.
- Broken imports: if `from some.module import Symbol` fails AFTER auto-install, do not use Symbol.
  Missing packages (reportlab, Pillow, etc.) are installed automatically — do not abandon a library
  solely because it was not pre-installed in the environment.
- google.generativeai multimodal: use image dicts {"mime_type": "image/jpeg", "data": image_bytes}
  in generate_content([image_dict, prompt_text]). Do NOT import Part from google.generativeai.types.
- Invalid JSON in your outer response: function_code must be one escaped string with \n for newlines.
- Using pass/TODO/placeholder instead of real logic — write the full implementation in one shot.
- Returning output keys not listed in this task's outputParameters.
- Using await (tools are synchronous).
- Deprecated LLM model ids (gemini-2.5-*, gemini-pro-vision, gpt-3.5-turbo, claude-2*, etc.).


PRE-SUBMISSION CHECKLIST (complete ALL before you respond — avoids automatic retry):
1. function_code is ONLY the _run body (no `def _run`, no class, no import lines in function_code).
2. Every import in the imports array must be valid Python. Missing third-party packages are auto-installed at validation/runtime (same as UI sandbox functions) — prefer real libraries (e.g. reportlab, Pillow) over inventing alternate APIs just to avoid imports.
3. No bare `pass`, TODO, NotImplementedError, mock data, or except: pass stubs.
4. return {{...}} must be a dict whose keys are EXACTLY the outputParameters names for this task.
5. parameters must include these input names: jd_id, candidate_count, job_title, location, min_years_experience, required_skills, preferred_skills, tools_and_technologies, education_level.
6. return dict must include these output keys: batch_id, fetched_count.
7. Secrets only from request_meta['credentials'] using exact configured credential names.
8. If function_code embeds JSON in an f-string, outer delimiter must be f'''...'''.
9. Mentally compile-check: the snippet must be valid Python when indented under def _run.


Return JSON in the SAME shape as this example (replace content; do not add extra top-level keys):
{
  "name": "ToolRegistryKey",
  "description": "What the tool does in one sentence.",
  "parameters": [
    {
      "name": "example_arg",
      "param_type": "Any",
      "description": "meaning of the input arg",
      "required": true
    }
  ],
  "outputParameters": [
    {
      "name": "result_value",
      "dataType": "Any",
      "description": "what the tool returns"
    }
  ],
  "keywords": [
    "gap_generated"
  ],
  "function_code": "return {\"result_value\": example_arg}",
  "imports": [
    "from typing import Any"
  ]
}

Example 1 — primality check (no credentials needed):
{
  "name": "check_prime",
  "description": "Checks whether the given integer is a prime number.",
  "parameters": [
    {"name": "n", "param_type": "Any", "description": "integer to test for primality", "required": true, "default_value": null}
  ],
  "outputParameters": [
    {"name": "is_prime", "dataType": "Boolean", "description": "True if n is prime, False otherwise"}
  ],
  "keywords": ["prime", "mathematics"],
  "function_code": "if n <= 1:\n    return {\"is_prime\": False}\nfor i in range(2, int(math.sqrt(n)) + 1):\n    if n % i == 0:\n        return {\"is_prime\": False}\nreturn {\"is_prime\": True}",
  "imports": ["import math", "from typing import Any"],
  "version": 1
}

Example 2 — hosted LLM call (illustrative shape only — YOU must pick provider/SDK from AVAILABLE PROJECT CREDENTIALS):
{
  "name": "translate_text",
  "description": "Translates text using whichever LLM credential the project provides.",
  "parameters": [
    {"name": "text", "param_type": "str", "description": "text to translate", "required": true, "default_value": null},
    {"name": "target_language", "param_type": "str", "description": "target language", "required": true, "default_value": null}
  ],
  "outputParameters": [
    {"name": "translated_text", "dataType": "Text", "description": "translated string"}
  ],
  "keywords": ["translation", "language"],
  "function_code": "creds = (request_meta or {}).get(\"credentials\") or {}\napi_key = creds.get(\"<use_exact_credential_name_from_list>\")\nif not api_key:\n    return {\"error\": \"Missing credential '<use_exact_credential_name_from_list>'.\"}\n# Call the provider SDK or REST API that matches that credential (Gemini/OpenAI/Claude/etc.)\n# ... production call here ...\nreturn {\"translated_text\": <result_text>}",
  "imports": ["from typing import Any"],
  "version": 1
}

Example 3 — MongoDB CRUD (imports are separate; function_code has NO imports):
{
  "name": "mongo_users_crud_example",
  "description": "Example CRUD patterns for a MongoDB 'users' collection using the platform Database helper.",
  "parameters": [
    {"name": "op", "param_type": "str", "description": "one of: create|get|update|delete", "required": true, "default_value": null},
    {"name": "user_id", "param_type": "str", "description": "logical user id (used as filter key in this example)", "required": false, "default_value": null},
    {"name": "payload", "param_type": "dict", "description": "document fields for create/update", "required": false, "default_value": null}
  ],
  "outputParameters": [
    {"name": "result", "dataType": "Any", "description": "operation-specific result payload"}
  ],
  "keywords": ["mongodb", "crud", "example"],
  "function_code": "coll = Database.get_sync_collection(\"users\")\nif op == \"create\":\n    doc = payload or {}\n    res = coll.insert_one(doc)\n    return {\"result\": {\"inserted_id\": str(res.inserted_id)}}\nif op == \"get\":\n    if not user_id:\n        return {\"result\": {\"error\": \"user_id is required for get\"}}\n    doc = coll.find_one({\"user_id\": user_id})\n    if not doc:\n        return {\"result\": None}\n    doc[\"_id\"] = str(doc.get(\"_id\"))\n    return {\"result\": doc}\nif op == \"update\":\n    if not user_id:\n        return {\"result\": {\"error\": \"user_id is required for update\"}}\n    updates = payload or {}\n    res = coll.update_one({\"user_id\": user_id}, {\"$set\": updates}, upsert=False)\n    return {\"result\": {\"matched\": int(res.matched_count), \"modified\": int(res.modified_count)}}\nif op == \"delete\":\n    if not user_id:\n        return {\"result\": {\"error\": \"user_id is required for delete\"}}\n    res = coll.delete_one({\"user_id\": user_id})\n    return {\"result\": {\"deleted\": int(res.deleted_count)}}\nreturn {\"result\": {\"error\": \"unknown op\", \"allowed\": [\"create\", \"get\", \"update\", \"delete\"]}}",
  "imports": ["from typing import Any", "from services.database import Database"],
  "version": 1
}

RULES — all are mandatory, violations cause rejection and retry:

1. "function_code" is ONLY the body of _run (do NOT define `def _run` or a `class`). Helper `def` functions nested inside the body are allowed.
   Write every line at column 0 — the registry indents automatically.
   It MUST NOT contain any import statements (no "import x" and no "from x import y"). All imports belong in the "imports" array only.
2. The function MUST return a plain dict whose keys exactly match the names in "outputParameters". This is how downstream nodes bind to the tool's outputs.
3. Every parameter in the IO contract's "inputParameters" MUST appear in "parameters" with the exact same "name".
   Do NOT add extra parameters that are not in the IO contract (no invented user_id, org_id, etc.).
   Read identity/audit fields from request_meta if needed — they are injected at runtime, not bound by the graph.
4. "outputParameters" MUST list every output key your function returns, with accurate "dataType" (Text, Number, Boolean, Any, Object, Array) and a short "description".
5. Use only safe standard-library logic unless a common import is truly necessary; keep code short and deterministic.
6. Include 'from typing import Any' in imports when using Any.
7. Respond with a single JSON object only (no markdown prose before/after). Required keys: name, description, parameters, outputParameters, function_code, imports.

8. ══ ABSOLUTE PROHIBITION ON MOCK / SIMULATION CODE ══
   The following are BANNED without exception — their presence causes automatic rejection:
   • Hardcoded fake values (invoice numbers, amounts, names, UUIDs, dates, coordinates, etc.)
   • Comments containing: "simulate", "mock", "placeholder", "demonstration", "in a real scenario",
     "assume successful", "for this example", "would involve", "would call"
   • Returning fabricated data structures that pretend to be real tool outputs
   • `raise NotImplementedError`, bare `pass` (any line that is only pass), TODO stubs
   • NEVER write `pass` — not even one line. Implement the full body immediately.
   • Any code whose correctness depends on inputs that are not in the io_contract
COMPLETE IMPLEMENTATION REQUIREMENTS:
    - For file upload: Actually save the file to provided path or return error if not possible    
    - For API calls: Actually make the HTTP request or return error about missing config or invalid request
    - For database: Actually query/insert/update or return error about missing collection or invalid query
    - For OCR/images/PDF: Actually process inputs (vision API if credentialed, else pytesseract/PIL, etc.)
    - NO "example" data, NO "mock" responses, NO "simulated" logic or placeholder code
    - Do NOT copy example provider names, models, or field names unless they appear in THIS task's io_contract

9. ══ MISSING CREDENTIALS → REAL CODE + RUNTIME ERROR (NEVER STUB) ══
   Do NOT return capability_gap, empty stubs, or "credentials not available" placeholders.
   Those are REJECTED automatically.

   Always write the real implementation. At runtime, if a required secret is absent:
        creds = (request_meta or {}).get("credentials") or {}
        api_key = creds.get("EXACT_CREDENTIAL_NAME")
        if not api_key:
            return {
                "error": "Missing credential 'EXACT_CREDENTIAL_NAME'. Ask admin to configure it.",
                # also include every outputParameters key with safe empty defaults
            }

   Optional integrations: if the task says the user chose PDF upload (and email/Drive are
   alternatives they did not select), implement ONLY PDF upload — do not require IMAP/Drive
   credentials for unused options.

10. For file/document processing: NEVER accept an opaque ID as the only input unless you can
    resolve it to bytes using a real service whose credentials are in the io_contract.
    Prefer accepting raw bytes (bytes param_type) or a base64 string, then use a real parser
    (pypdf, pdfplumber, python-docx, Pillow, etc.).

11. ══ CREDENTIAL & SECRET ACCESS ══
    • NEVER hardcode API keys, tokens, passwords, or any secret values in function_code.
    • NEVER read secrets from os.environ in function_code.
    • The _run method automatically receives a `request_meta` dict injected by the platform.
    • Read ALL secrets like this (use the exact credential name from the AVAILABLE PROJECT CREDENTIALS list above):
        creds = (request_meta or {}).get("credentials") or {}
        api_key = creds.get("exact_credential_name")
        if not api_key:
            return {"error": "Missing credential 'exact_credential_name'. Ask admin to configure it."}
    • `request_meta` is injected at runtime — do NOT add it to the "parameters" list in your JSON output.
      It is always available inside function_code as a local variable.

12. ══ F-STRINGS WITH EMBEDDED JSON ══
    When function_code uses an f-string whose literal portion contains JSON examples or other double-quoted text,
    ALWAYS delimit the outer f-string with triple single quotes: f'''...''' (never f"""...""" or f"...").
    This prevents quote collisions and SyntaxError when JSON keys/values use double quotes.
    Example (correct):
      prompt = f'''Analyze this payload and return JSON:
      {"status": "ok", "items": {items}}
      Input: {user_text}'''
    Example (wrong — will break parsing):
      prompt = f"Return {\"status\": \"ok\"} for {user_text}"

13. ══ NO HARDCODED VENDORS OR OUTPUT SHAPES ══
    Examples in this prompt are structural templates only. Your code MUST follow the user's task,
    io_contract field names, and AVAILABLE PROJECT CREDENTIALS — never blindly copy example SDKs,
    model IDs, credential names, or business logic from unrelated examples.

14. ══ FIRST-ATTEMPT QUALITY ══
    Your response is compile-checked immediately. Produce complete, runnable code on the first try.
    Do not submit a skeleton to be "filled in later". If unsure about a library symbol, use a pattern
    known to work (stdlib, requests, dict-based multimodal payloads) rather than experimental imports.

15. ══ MODEL IDS FROM RESEARCH ══
    Any GenerativeModel("..."), model=, or API model parameter in function_code MUST use a current model id
    from the MODEL RESEARCH section (Google Search). Known deprecated families are auto-rejected.

Remember: 100% production-ready code only. Missing credentials → runtime Missing credential error, NEVER a capability_gap stub.
```


### Raw LLM response (full)

_File:_ `tool_gap_llm_raw.txt` (1174 chars) — truncated mid-JSON; never reached `function_code`.

```
```json
{
  "name": "linkedin_sourcing_trace_tool",
  "description": "Backend Tool: LinkedIn Sourcing & Scoring Tool. Generates search queries, resolves filters, fetches profiles, maps to schema, and scores candidates based on JD requirements.",
  "parameters": [
    {
      "name": "jd_id",
      "param_type": "string",
      "description": "The MongoDB ObjectId of the Job Description to source for.",
      "required": true
    },
    {
      "name": "candidate_count",
      "param_type": "number",
      "description": "The desired number of candidates to source.",
      "required": true
    },
    {
      "name": "job_title",
      "param_type": "string",
      "description": "Official title or designation of the position.",
      "required": true
    },
    {
      "name": "location",
      "param_type": "string",
      "description": "Primary physical location (city, state, country).",
      "required": true
    },
    {
      "name": "min_years_experience",
      "param_type": "number",
      "description": "Minimum required years of relevant experience.",
      "required": true
    },
    {
      "name": "required_skills",
      "param_type": "list",
```


### Parsed tool generation result (full)

```json
{
  "name": "linkedin_sourcing_trace_tool",
  "description": "Backend Tool: LinkedIn Sourcing & Scoring Tool. Generates search queries, resolves filters, fetches profiles, maps to schema, and scores candidates based on JD requirements.",
  "parameters": [
    {
      "name": "jd_id",
      "param_type": "string",
      "description": "The MongoDB ObjectId of the Job Description to source for.",
      "required": true,
      "default_value": null
    },
    {
      "name": "candidate_count",
      "param_type": "float",
      "description": "The desired number of candidates to source.",
      "required": true,
      "default_value": null
    },
    {
      "name": "job_title",
      "param_type": "string",
      "description": "Official title or designation of the position.",
      "required": true,
      "default_value": null
    },
    {
      "name": "location",
      "param_type": "string",
      "description": "Primary physical location (city, state, country).",
      "required": true,
      "default_value": null
    },
    {
      "name": "min_years_experience",
      "param_type": "float",
      "description": "Minimum required years of relevant experience.",
      "required": true,
      "default_value": null
    },
    {
      "name": "required_skills",
      "param_type": "list",
      "description": "Used to generate search queries and for scoring candidate skills.",
      "required": true,
      "default_value": null
    },
    {
      "name": "preferred_skills",
      "param_type": "list",
      "description": "Used to generate search queries and for scoring candidate skills.",
      "required": true,
      "default_value": null
    },
    {
      "name": "tools_and_technologies",
      "param_type": "list",
      "description": "Used to generate search queries and for scoring candidate skills.",
      "required": true,
      "default_value": null
    },
    {
      "name": "education_level",
      "param_type": "string",
      "description": "Minimum academic qualification, used for scoring.",
      "required": true,
      "default_value": null
    }
  ],
  "outputParameters": [
    {
      "name": "batch_id",
      "dataType": "Any",
      "required": true,
      "description": "Identifier for the sourcing batch, same as the input jd_id."
    },
    {
      "name": "fetched_count",
      "dataType": "Any",
      "required": true,
      "description": "The actual number of candidate profiles fetched and saved."
    }
  ],
  "function_code": ""
}
```


### Code generated

- `function_code` chars: **0** (empty)
- parameters: 9
- outputParameters: 2
- Mentions in function_code: Unipile=False, api20=False, linkedin_sections=False, scoring=False (vacuously — no code)

```python
# (empty function_code)
```


---

## 7. Gap UI screen → AIProcessor (enhance + IR + dataSource enricher)

**Node:** `candidate_profiles_screen`

### 7a. Prompt / node description sent into AIProcessor (full)

_File:_ `ui_gap_prompt_to_ai_processor.txt` (2318 chars)

This is the frozen **intentDescription** plus the typed workflow I/O contract:

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
```


### 7b. Prompt enhancer result (full)

_File:_ `ui_enhanced_prompt.txt` (8629 chars)

- Contains deterministic block `ORIGINAL NODE SPECIFICATION`: **True**
- Intent substring retained in enhanced prompt: **True** (live-trace check)

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

ENHANCED / ELABORATED REQUIREMENTS (additive only; do not drop or contradict the original):
Create a **Candidate Profiles UI** screen designed for recruiters to review and filter a list of candidates matched to a specific job description. This screen serves as a review dashboard where users can evaluate candidate scores, experience, and skills before finalizing the review process.

### 1. Core Functionality & Data Fetching
The screen must fetch and display data from the `all_Candidates` collection.
- **Query Logic:** Fetch documents where `jd_id` matches the provided `batch_id` (passed as a workflow input).
- **Sorting:** By default, results must be sorted by `score` in descending order.
- **Database Field Naming:** The system should first inspect the `all_Candidates` collection schema to determine the actual field names (e.g., handling variations like `jd_id` vs `job_id` or `score` vs `candidate_score`) and use the exact names found in the database for queries.
- **Data Flow:** The screen receives `batch_id`, `fetched_count`, and `candidate_count` as inputs and produces a `review_completed` boolean output.

### 2. Layout & Design Direction
- **Layout:** Use a full-viewport desktop layout (max-width: 1280px or full-bleed) to accommodate a grid or list of candidate cards.
- **Visual Hierarchy:** Prioritize the candidate's name and match score. Use a clean, professional aesthetic that emphasizes readability and data clarity.
- **Responsiveness:** The layout must adapt from a multi-column desktop view to a single-column mobile view.

### 3. List-Level UI Components
- **Header:** A dynamic header displaying: "Showing {fetched_count} of {candidate_count} requested".
- **Controls Bar:**
    - **Sort Toggle:** A dropdown menu allowing users to re-sort the list by 'Score' (default), 'Experience' (mapped to `total_years_of_experience`), or 'Network Distance'.
    - **Filter:** A number input or slider for 'Minimum Score Threshold' (range 0-100). Adjusting this should dynamically filter the visible candidate cards based on their `score`.

### 4. Profile Card UI (Repeated for each candidate)
Each candidate is represented by a card containing the following sections:
- **Header Area:**
    - Circular avatar using `profile_picture_url` (fallback to initials if the URL is null).
    - Full Name: Concatenation of `first_name` and `last_name`.
    - LinkedIn Integration: A LinkedIn icon linking to `linkedin_profile_url` (opening in a new tab).
    - Subtext: Display `headline` and `location`.
- **Scoring Area:**
    - **Score Badge:** Display the value as `score / 100`. 
    - **Color Logic:** Apply Green if score >= 70, Orange if 40-69, and Red if < 40.
    - **Score Breakdown Bar:** A segmented visual progress bar representing the five components from the `score_breakdown` object: Skills, Experience, Education, Open to Work, and Contact.
- **Key Highlights:**
    - **Experience:** Display `total_years_of_experience` followed by the text " Years".
    - **Status Badge:** Render a prominent "Open to Work" badge only if `actively_looking` == "Yes".
    - **Matched Skills:** Render the `matched_skills` array as a series of highlighted chips or tags.
- **Contact Info:**
    - Display `email` and `phone`. Include a copy-to-clipboard icon next to each.
    - If a value is null, display "Not provided".
- **Experience Summary:**
    - A brief list showing the top 2-3 most recent entries from the `experience` array, formatted as: "Company | Position | Duration".

### 5. User Interactions & Feedback
- **Hover States:** Interactive elements like cards, buttons, and icons should have subtle hover effects.
- **Copy Feedback:** Provide a brief visual confirmation (e.g., a tooltip or toast) when contact info is copied to the clipboard.
- **Loading & Empty States:** Show a loading indicator during the initial data fetch from `all_Candidates`. If no candidates match the filters, display a clear "No candidates found" message.
- **Validation:** Ensure the 'Minimum Score Threshold' input only accepts values between 0 and 100.

### 6. Actions & Workflow Integration
- **Primary Action:** A 'Finish Review' button located at the bottom or in a fixed footer.
- **Submission:** Upon clicking 'Finish Review', the system must set `review_completed` to `true` and transition to the next node in the workflow.

### 7. Workflow IO Contract
**Input Parameters:**
- `batch_id` (string, required): The ID used to filter candidates.
- `fetched_count` (number, required): The number of records retrieved.
- `candidate_count` (number, required): The total number of candidates requested.

**Output Parameters:**
- `review_completed` (boolean, required): Set to `true` upon clicking the primary action button.

**Workflow Context:**
- Step ID: `candidate_profiles_screen`
- No dependencies on previous step outputs beyond the defined input parameters.

            INPUT-OUTPUT CONTRACT:
            The following parameters have been identified for workflow orchestration:

            INPUT PARAMETERS (from previous steps):
            [
  {
    "name": "batch_id",
    "type": "string",
    "description": "The unique identifier used to filter candidates from the all_Candidates collection (mapped to jd_id)",
    "required": true,
    "source": "previous_screen",
    "example": "jd_9921_recruitment_batch"
  },
  {
    "name": "fetched_count",
    "type": "number",
    "description": "The number of candidate records successfully retrieved for the current view",
    "required": true,
    "source": "previous_screen",
    "example": 25
  },
  {
    "name": "candidate_count",
    "type": "number",
    "description": "The total number of candidates requested for the specific job description",
    "required": true,
    "source": "previous_screen",
    "example": 100
  }
]

            OUTPUT PARAMETERS (for next steps):
            [
  {
    "name": "review_completed",
    "type": "boolean",
    "description": "Status flag set to true when the recruiter clicks the Finish Review button",
    "example": true
  }
]

            These parameters must be correctly implemented in the IR schema's inputParameters and outputParameters arrays.
```


### 7c. AIProcessor IR schema (full)

_File:_ `ui_ir_schema.json`

```json
{
  "screen": {
    "id": "candidate_profiles_screen",
    "type": "form",
    "title": "Candidate Profiles Dashboard",
    "description": "High-density data visualization and review interface for candidate profiles associated with a specific job description batch.",
    "route": "/candidates/review",
    "selector": "app-candidate-profiles"
  },
  "theme": {
    "direction": "Data-Dense Dashboard",
    "primary": "#059669",
    "secondary": "#10B981",
    "accent": "#D97706",
    "background": "#ECFDF5",
    "text": "#0F172A",
    "error": "#EF4444",
    "success": "#10B981",
    "warning": "#F59E0B",
    "info": "#3B82F6",
    "typography": {
      "display": "Plus Jakarta Sans",
      "body": "Plus Jakarta Sans"
    },
    "effects": [
      "Large flowing CSS/SVG gradients, subtle 8-12s animations, depth via color layering, smooth morph"
    ],
    "radius": "12px",
    "spacing": "16px",
    "shadow": "0 10px 15px -3px rgba(0, 0, 0, 0.05), 0 4px 6px -2px rgba(0, 0, 0, 0.025)"
  },
  "antiPatterns": [
    "Flat design without depth",
    "Text-heavy pages"
  ],
  "layout": {
    "mode": "single",
    "columns": 1,
    "gap": "24px",
    "max_width": "1280px"
  },
  "components": [
    {
      "id": "header_section",
      "type": "Header",
      "label": "Dashboard Header",
      "props": {
        "title": "Candidate Profiles Review",
        "subtitle": "Showing 0 of 0 requested",
        "fetchedCount": 0,
        "candidateCount": 0,
        "className": "text-[#0F172A] font-display"
      },
      "position": {
        "order": 1,
        "width": "100%",
        "visible": true
      },
      "style": {
        "bg": "transparent",
        "padding": "12px 0px",
        "radius": "0px",
        "responsive": {
          "tablet": {
            "padding": "12px 0px"
          },
          "mobile": {
            "padding": "8px 0px"
          }
        }
      }
    },
    {
      "id": "controls_bar",
      "type": "ControlsBar",
      "label": "Filter and Sort Controls",
      "props": {
        "sortOptions": [
          {
            "label": "Score",
            "value": "score"
          },
          {
            "label": "Experience",
            "value": "total_years_of_experience"
          },
          {
            "label": "Network Distance",
            "value": "network_distance"
          }
        ],
        "defaultSort": "score",
        "minScoreLabel": "Minimum Score Threshold",
        "minScoreValue": 0,
        "minScoreMin": 0,
        "minScoreMax": 100,
        "className": "bg-[#FFFFFF] border-[#E2E8F0] shadow-sm rounded-[12px]"
      },
      "position": {
        "order": 2,
        "width": "100%",
        "visible": true
      },
      "style": {
        "bg": "#FFFFFF",
        "border": "1px solid #E2E8F0",
        "shadow": "sm",
        "padding": "12px 20px",
        "radius": "12px",
        "responsive": {
          "tablet": {
            "padding": "16px"
          },
          "mobile": {
            "padding": "12px"
          }
        }
      }
    },
    {
      "id": "candidate_list",
      "type": "CandidateCardList",
      "label": "Candidate Profiles Grid",
      "props": {
        "candidates": [],
        "emptyStateMessage": "No candidates match the selected criteria.",
        "loading": false,
        "className": "grid gap-4"
      },
      "position": {
        "order": 3,
        "width": "100%",
        "visible": true
      },
      "style": {
        "bg": "transparent",
        "padding": "0px",
        "radius": "0px",
        "responsive": {
          "tablet": {},
          "mobile": {}
        }
      }
    },
    {
      "id": "footer_actions",
      "type": "FooterActions",
      "label": "Review Actions",
      "props": {
        "primaryButtonLabel": "Finish Review",
        "primaryButtonVariant": "solid",
        "primaryButtonColor": "#059669",
        "className": "bg-[#FFFFFF] border-[#E2E8F0] shadow-md rounded-[12px]"
      },
      "position": {
        "order": 4,
        "width": "100%",
        "visible": true
      },
      "style": {
        "bg": "#FFFFFF",
        "border": "1px solid #E2E8F0",
        "shadow": "md",
        "padding": "16px 20px",
        "radius": "12px",
        "responsive": {
          "tablet": {
            "padding": "16px"
          },
          "mobile": {
            "padding": "12px"
          }
        }
      }
    }
  ],
  "dataModels": {
    "ScoreBreakdown": {
      "skills": {
        "type": "int",
        "required": true
      },
      "experience": {
        "type": "int",
        "required": true
      },
      "education": {
        "type": "int",
        "required": true
      },
      "open_to_work": {
        "type": "int",
        "required": true
      },
      "contact": {
        "type": "int",
        "required": true
      }
    },
    "CandidateExperience": {
      "company": {
        "type": "string",
        "required": true
      },
      "position": {
        "type": "string",
        "required": true
      },
      "duration": {
        "type": "string",
        "required": true
      }
    },
    "Candidate": {
      "id": {
        "type": "string",
        "required": true
      },
      "first_name": {
        "type": "string",
        "required": true
      },
      "last_name": {
        "type": "string",
        "required": true
      },
      "profile_picture_url": {
        "type": "string",
        "required": false
      },
      "linkedin_profile_url": {
        "type": "string",
        "required": false
      },
      "headline": {
        "type": "string",
        "required": false
      },
      "location": {
        "type": "string",
        "required": false
      },
      "score": {
        "type": "int",
        "required": true
      },
      "score_breakdown": {
        "type": "object",
        "required": true
      },
      "total_years_of_experience": {
        "type": "int",
        "required": true
      },
      "actively_looking": {
        "type": "string",
        "required": true
      },
      "matched_skills": {
        "type": "list",
        "required": true,
        "items": {
          "type": "string"
        }
      },
      "email": {
        "type": "string",
        "required": false
      },
      "phone": {
        "type": "string",
        "required": false
      },
      "experience": {
        "type": "list",
        "required": true,
        "items": {
          "model": "CandidateExperience"
        }
      }
    },
    "FetchCandidatesRequest": {
      "batch_id": {
        "type": "string",
        "required": true
      },
      "min_score": {
        "type": "int",
        "required": false
      },
      "sort_by": {
        "type": "string",
        "required": false
      }
    },
    "FetchCandidatesResponse": {
      "candidates": {
        "type": "list",
        "required": false,
        "items": {
          "model": "Candidate"
        }
      },
      "error": {
        "type": "string",
        "required": false
      }
    },
    "SubmitReviewRequest": {
      "batch_id": {
        "type": "string",
        "required": true
      },
      "review_completed": {
        "type": "bool",
        "required": true
      }
    },
    "SubmitReviewResponse": {
      "success": {
        "type": "bool",
        "required": false
      },
      "error": {
        "type": "string",
        "required": false
      }
    }
  },
  "dataSources": [
    {
      "id": "fetch_candidates",
      "description": "Fetches candidate profiles from the all_Candidates collection for a specific job description batch. Applies dynamic score filtering and sorting based on user input.",
      "type": "dynamic",
      "inputModel": "FetchCandidatesRequest",
      "responseModel": "FetchCandidatesResponse",
      "config": {
        "method": "POST",
        "timeoutMs": 15000
      },
      "operation": {
        "action": "find",
        "collection": "all_Candidates",
        "description": "Find candidates where jd_id matches batch_id, sorted by score descending by default, with schema probing for exact field names.",
        "readCollections": [
          "all_Candidates"
        ],
        "writeCollections": [],
        "schemaProbe": true,
        "inputFieldMappings": {
          "batch_id": "jd_id",
          "min_score": "score"
        },
        "outputFieldMappings": {
          "candidates": "candidates"
        }
      },
      "databaseLogic": "from services.database import Database\n\n1. Validate `params.batch_id` is provided. If missing, return `{\"error\": \"batch_id is required\"}`.\n2. Runtime: inspect the `all_Candidates` collection to discover actual field names before querying. Resolve casing (e.g., jd_id vs jdId, score vs Score, total_years_of_experience vs totalYearsOfExperience) from the live schema. Do NOT assume field names.\n3. Construct the query filter: match the resolved `jd_id` field to `params.batch_id`.\n4. If `params.min_score` is provided (0-100), append a filter condition where the resolved `score` field is `$gte` `params.min_score`.\n5. Determine sorting logic based on `params.sort_by`:\n   - If 'Experience', sort by resolved `total_years_of_experience` DESC.\n   - If 'Network Distance', sort by resolved `network_distance` ASC.\n   - Default: sort by resolved `score` DESC.\n6. Execute `await Database.find(\"all_Candidates\", query, sort=sort_logic)`.\n7. Map the raw documents to the `Candidate` responseModel schema. Ensure nested objects like `score_breakdown` and arrays like `matched_skills` and `experience` are properly mapped. Handle nulls gracefully for optional fields (`profile_picture_url`, `linkedin_profile_url`, `headline`, `location`, `email`, `phone`).\n8. Return `{\"candidates\": mapped_results}`. On database timeout or failure, catch the exception and return `{\"error\": str(e)}`."
    },
    {
      "id": "submit_review",
      "description": "Records the completion of the candidate review process for a specific batch, updating the batch status to allow workflow progression.",
      "type": "dynamic",
      "inputModel": "SubmitReviewRequest",
      "responseModel": "SubmitReviewResponse",
      "config": {
        "method": "POST",
        "timeoutMs": 10000
      },
      "operation": {
        "action": "update",
        "collection": "batch_reviews",
        "description": "Updates or inserts the review completion status for the batch in the batch_reviews collection.",
        "readCollections": [],
        "writeCollections": [
          "batch_reviews"
        ],
        "schemaProbe": true,
        "inputFieldMappings": {
          "batch_id": "batch_id",
          "review_completed": "review_completed"
        },
        "outputFieldMappings": {
          "success": "success"
        }
      },
      "databaseLogic": "from services.database import Database\nfrom datetime import datetime, timezone\n\n1. Validate `params.batch_id` and `params.review_completed` are provided. If missing, return `{\"success\": False, \"error\": \"Missing required parameters\"}`.\n2. Construct the update payload as a plain dictionary (per platform rules, never use {\"$set\": ...}):\n   `payload = {\"batch_id\": params.batch_id, \"review_completed\": params.review_completed, \"updated_at\": datetime.now(timezone.utc).isoformat()}`.\n3. Execute `await Database.update_one(\"batch_reviews\", {\"batch_id\": params.batch_id}, payload, upsert=True)`.\n4. Return `{\"success\": True}`. On database error or timeout, catch the exception, log it, and return `{\"success\": False, \"error\": str(e)}`."
    }
  ],
  "inputParameters": [
    {
      "name": "batch_id",
      "type": "string",
      "description": "The unique identifier used to filter candidates from the all_Candidates collection for a specific job description.",
      "required": true,
      "source": {
        "type": "previous_screen"
      },
      "example": "batch_98765_jd_001",
      "bindTo": [
        {
          "target": "dataSource:fetch_candidates",
          "path": "config.params.batch_id"
        },
        {
          "target": "state:batch_id",
          "path": ""
        }
      ]
    },
    {
      "name": "fetched_count",
      "type": "number",
      "description": "The number of candidate records successfully retrieved for the current batch to be displayed in the header.",
      "required": true,
      "source": {
        "type": "previous_screen"
      },
      "example": 45,
      "bindTo": [
        {
          "target": "component:header_section",
          "path": "props.fetchedCount"
        }
      ]
    },
    {
      "name": "candidate_count",
      "type": "number",
      "description": "The total number of candidates that were originally requested or available for this batch.",
      "required": true,
      "source": {
        "type": "previous_screen"
      },
      "example": 100,
      "bindTo": [
        {
          "target": "component:header_section",
          "path": "props.candidateCount"
        }
      ]
    },
    {
      "name": "review_completed",
      "type": "bool",
      "description": "Flag indicating that the user has finished the review process and is ready to move to the next workflow step.",
      "required": true,
      "source": {
        "type": "previous_screen"
      },
      "example": true,
      "bindTo": [
        {
          "target": "dataSource:submit_review",
          "path": "config.params.review_completed"
        },
        {
          "target": "state:review_completed",
          "path": ""
        }
      ]
    }
  ],
  "outputParameters": [
    {
      "name": "review_completed",
      "type": "bool",
      "description": "Flag indicating that the user has finished the review process and is ready to move to the next workflow step.",
      "example": true,
      "extractFrom": {
        "dataSourceId": "submit_review",
        "responsePath": "success"
      }
    }
  ],
  "state": {
    "bindings": {
      "batch_id": "workflowContext.batch_id",
      "fetched_count": "workflowContext.fetched_count",
      "candidate_count": "workflowContext.candidate_count",
      "candidates": "dataSource:fetch_candidates.response.candidates",
      "review_completed": "workflowContext.review_completed"
    }
  }
}
```


### 7d. DataSource description / databaseLogic enricher results (full)

_File:_ `ui_datasource_specs.json`

IR screen summary from live trace:

```json
{
  "id": "candidate_profiles_screen",
  "type": "form",
  "title": "Candidate Profiles Dashboard",
  "description": "High-density data visualization and review interface for candidate profiles associated with a specific job description batch.",
  "route": "/candidates/review",
  "selector": "app-candidate-profiles"
}
```


Enriched dataSources (description + Step 5.5 `databaseLogic`):

```json
[
  {
    "id": "fetch_candidates",
    "type": "dynamic",
    "description_chars": 164,
    "databaseLogic_chars": 1376,
    "description": "Fetches candidate profiles from the all_Candidates collection for a specific job description batch. Applies dynamic score filtering and sorting based on user input.",
    "databaseLogic": "from services.database import Database\n\n1. Validate `params.batch_id` is provided. If missing, return `{\"error\": \"batch_id is required\"}`.\n2. Runtime: inspect the `all_Candidates` collection to discover actual field names before querying. Resolve casing (e.g., jd_id vs jdId, score vs Score, total_years_of_experience vs totalYearsOfExperience) from the live schema. Do NOT assume field names.\n3. Construct the query filter: match the resolved `jd_id` field to `params.batch_id`.\n4. If `params.min_score` is provided (0-100), append a filter condition where the resolved `score` field is `$gte` `params.min_score`.\n5. Determine sorting logic based on `params.sort_by`:\n   - If 'Experience', sort by resolved `total_years_of_experience` DESC.\n   - If 'Network Distance', sort by resolved `network_distance` ASC.\n   - Default: sort by resolved `score` DESC.\n6. Execute `await Database.find(\"all_Candidates\", query, sort=sort_logic)`.\n7. Map the raw documents to the `Candidate` responseModel schema. Ensure nested objects like `score_breakdown` and arrays like `matched_skills` and `experience` are properly mapped. Handle nulls gracefully for optional fields (`profile_picture_url`, `linkedin_profile_url`, `headline`, `location`, `email`, `phone`).\n8. Return `{\"candidates\": mapped_results}`. On database timeout or failure, catch the exception and return `{\"error\": str(e)}`.",
    "operation": {
      "action": "find",
      "collection": "all_Candidates",
      "description": "Find candidates where jd_id matches batch_id, sorted by score descending by default, with schema probing for exact field names.",
      "readCollections": [
        "all_Candidates"
      ],
      "writeCollections": [],
      "schemaProbe": true,
      "inputFieldMappings": {
        "batch_id": "jd_id",
        "min_score": "score"
      },
      "outputFieldMappings": {
        "candidates": "candidates"
      }
    },
    "config": {
      "method": "POST",
      "timeoutMs": 15000
    }
  },
  {
    "id": "submit_review",
    "type": "dynamic",
    "description_chars": 133,
    "databaseLogic_chars": 730,
    "description": "Records the completion of the candidate review process for a specific batch, updating the batch status to allow workflow progression.",
    "databaseLogic": "from services.database import Database\nfrom datetime import datetime, timezone\n\n1. Validate `params.batch_id` and `params.review_completed` are provided. If missing, return `{\"success\": False, \"error\": \"Missing required parameters\"}`.\n2. Construct the update payload as a plain dictionary (per platform rules, never use {\"$set\": ...}):\n   `payload = {\"batch_id\": params.batch_id, \"review_completed\": params.review_completed, \"updated_at\": datetime.now(timezone.utc).isoformat()}`.\n3. Execute `await Database.update_one(\"batch_reviews\", {\"batch_id\": params.batch_id}, payload, upsert=True)`.\n4. Return `{\"success\": True}`. On database error or timeout, catch the exception, log it, and return `{\"success\": False, \"error\": str(e)}`.",
    "operation": {
      "action": "update",
      "collection": "batch_reviews",
      "description": "Updates or inserts the review completion status for the batch in the batch_reviews collection.",
      "readCollections": [],
      "writeCollections": [
        "batch_reviews"
      ],
      "schemaProbe": true,
      "inputFieldMappings": {
        "batch_id": "batch_id",
        "review_completed": "review_completed"
      },
      "outputFieldMappings": {
        "success": "success"
      }
    },
    "config": {
      "method": "POST",
      "timeoutMs": 10000
    }
  }
]
```


---

## 8. Backend generator path

Continuation analyzed IR dataSources for grounding / codegen. Full backend FastAPI emission was **not** completed in this run.

- Backend error during continuation attempt: `cannot import name '_normalize_update_doc' from 'services.database' (unknown location)`

### What was passed into the backend path

The enriched dataSources from §7d (`fetch_candidates`, `submit_review`), both `type=dynamic` (Mongo), with thick `databaseLogic` and **no** external endpoint URL.

### Backend DS analysis result (full)

_File:_ `backend_ds_analysis.json`

```json
[
  {
    "id": "fetch_candidates",
    "type": "dynamic",
    "databaseLogic_chars": 1376,
    "has_endpoint_url": false,
    "databaseLogic_preview": "from services.database import Database\n\n1. Validate `params.batch_id` is provided. If missing, return `{\"error\": \"batch_id is required\"}`.\n2. Runtime: inspect the `all_Candidates` collection to discover actual field names before querying. Resolve casing (e.g., jd_id vs jdId, score vs Score, total_years_of_experience vs totalYearsOfExperience) from the live schema. Do NOT assume field names.\n3. Construct the query filter: match the resolved `jd_id` field to `params.batch_id`.\n4. If `params.min_score` is provided (0-100), append a filter condition where the resolved `score` field is `$gte` `params.min_score`.\n5. Determine sorting logic based on `params.sort_by`:\n   - If 'Experience', sort by resolved `total_years_of_experience` DESC.\n   - If 'Network Distance', sort by resolved `network_distance` ASC.\n   - Default: sort by resolved `score` DESC.\n6. Execute `await Database.find(\"all_Candidates\", query, sort=sort_logic)`.\n7. Map the raw documents to the `Candidate` responseModel schema. Ensure nested objects like `score_breakdown` and arrays like `matched_skills` and `experience` are properly mapped. Handle nulls gracefully for optional fields (`profile_picture_url`, `linkedin_profile_url`, `headline`, `location`, `email`, `phone`).\n8. Return `{\"candidates\": mapped_results}`. On database timeout or failure, catch the exception and return `{\"error\": str(e)}`.",
    "grounding": null
  },
  {
    "id": "submit_review",
    "type": "dynamic",
    "databaseLogic_chars": 730,
    "has_endpoint_url": false,
    "databaseLogic_preview": "from services.database import Database\nfrom datetime import datetime, timezone\n\n1. Validate `params.batch_id` and `params.review_completed` are provided. If missing, return `{\"success\": False, \"error\": \"Missing required parameters\"}`.\n2. Construct the update payload as a plain dictionary (per platform rules, never use {\"$set\": ...}):\n   `payload = {\"batch_id\": params.batch_id, \"review_completed\": params.review_completed, \"updated_at\": datetime.now(timezone.utc).isoformat()}`.\n3. Execute `await Database.update_one(\"batch_reviews\", {\"batch_id\": params.batch_id}, payload, upsert=True)`.\n4. Return `{\"success\": True}`. On database error or timeout, catch the exception, log it, and return `{\"success\": False, \"error\": str(e)}`.",
    "grounding": null
  }
]
```


### Code the backend generator gave

**No full generated backend handler module was produced in this live trace.**

What *was* available as the executable logic payload for backend generation is the enriched `databaseLogic` itself:

#### `fetch_candidates` (`type=dynamic`, grounding=None)

- `has_endpoint_url`: False
- `databaseLogic` chars: 1376

```python
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


#### `submit_review` (`type=dynamic`, grounding=None)

- `has_endpoint_url`: False
- `databaseLogic` chars: 730

```python
from services.database import Database
from datetime import datetime, timezone

1. Validate `params.batch_id` and `params.review_completed` are provided. If missing, return `{"success": False, "error": "Missing required parameters"}`.
2. Construct the update payload as a plain dictionary (per platform rules, never use {"$set": ...}):
   `payload = {"batch_id": params.batch_id, "review_completed": params.review_completed, "updated_at": datetime.now(timezone.utc).isoformat()}`.
3. Execute `await Database.update_one("batch_reviews", {"batch_id": params.batch_id}, payload, upsert=True)`.
4. Return `{"success": True}`. On database error or timeout, catch the exception, log it, and return `{"success": False, "error": str(e)}`.
```


Grounding was correctly **not** invoked (Mongo `dynamic`, not `external`).

---

## 9. Char-length transformation summary

| Node | Original | Stage 1 | Stage 1.2 / 2a / 4 intent | Gap UI enhanced | Tool function_code |
|---|---:|---:|---:|---:|---:|
| jd_input_screen | 3189 | 1554 | 3642 | — | — |
| linkedin_sourcing_tool | 7947 | 1603 | 4597 | — | **0** |
| candidate_profiles_screen | 829 | 787 | 1840 | 8629 | — |

| DataSource | description chars | databaseLogic chars |
|---|---:|---:|
| `fetch_candidates` | 164 | 1376 |
| `submit_review` | 133 | 730 |

---

## Artifact index

- `00_original_steps.json` (1365 bytes)
- `REPORT.md` (90041 bytes)
- `backend_ds_analysis.json` (2519 bytes)
- `backend_error.txt` (86 bytes)
- `descriptions_stage1.json` (5574 bytes)
- `descriptions_stage1_2.json` (11986 bytes)
- `descriptions_stage1_7.json` (11986 bytes)
- `descriptions_stage2a.json` (11986 bytes)
- `descriptions_stage4c.json` (11986 bytes)
- `final_graph.json` (74214 bytes)
- `run.log` (36438 bytes)
- `scratch_01_stage1-skeleton-planner_response.json` (10560 bytes)
- `scratch_01_stage1-skeleton-planner_system.txt` (10047 bytes)
- `scratch_01_stage1-skeleton-planner_user.txt` (15085 bytes)
- `scratch_02_stage1_2-description-enricher_response.json` (21824 bytes)
- `scratch_02_stage1_2-description-enricher_system.txt` (8945 bytes)
- `scratch_02_stage1_2-description-enricher_user.txt` (18261 bytes)
- `scratch_03_stage2a-node-schema-builder_response.json` (20348 bytes)
- `scratch_03_stage2a-node-schema-builder_system.txt` (10642 bytes)
- `scratch_03_stage2a-node-schema-builder_user.txt` (12032 bytes)
- `scratch_04_stage2a-node-schema-builder_response.json` (13217 bytes)
- `scratch_04_stage2a-node-schema-builder_system.txt` (10642 bytes)
- `scratch_04_stage2a-node-schema-builder_user.txt` (12430 bytes)
- `scratch_05_stage4c-persistence-extract_response.json` (19041 bytes)
- `scratch_05_stage4c-persistence-extract_system.txt` (1172 bytes)
- `scratch_05_stage4c-persistence-extract_user.txt` (24095 bytes)
- `scratch_llm_calls.json` (1049 bytes)
- `state_stage1.json` (19626 bytes)
- `state_stage1_2.json` (26039 bytes)
- `state_stage1_7.json` (26038 bytes)
- `state_stage2a.json` (46666 bytes)
- `state_stage4c.json` (139011 bytes)
- `tool_error.txt` (24 bytes)
- `tool_gap_llm_raw.txt` (1174 bytes)
- `tool_gap_parsed.json` (2502 bytes)
- `tool_gap_prompt.txt` (25094 bytes)
- `tool_result_summary.json` (628 bytes)
- `ui_datasource_specs.json` (3982 bytes)
- `ui_enhanced_prompt.txt` (8631 bytes)
- `ui_gap_prompt_to_ai_processor.txt` (2318 bytes)
- `ui_ir_schema.json` (14322 bytes)

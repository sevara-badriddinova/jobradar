# Slice 6 (plan step 7): your candidate profile

**Goal:** the matcher needs to know who you are. Your resume is read **once**
and turned into a structured profile, and you add what a resume can't say in
`profile.toml`.

```
resume.pdf ──► text (pypdf) ──► Claude, structured output ──► CandidateProfile ──► candidate_profiles (v1, v2, …)
profile.toml [preferences] ───────────────────────────────────────────────────────► (read at match time)
```

## Using it

```bash
export ANTHROPIC_API_KEY=sk-ant-...            # https://console.anthropic.com
uv run jobradar init-db                        # adds the candidate_profiles table
uv run jobradar profile extract ~/Documents/resume.pdf
uv run jobradar profile show                   # check what it understood
```

If the extraction gets something wrong, say so in `[preferences].notes`.
The matcher reads both.

## Decisions and why

**Structured output, not free text.** The model must return JSON matching
`CandidateProfile` (a Pydantic model). The API enforces the schema, and the
SDK validates it again on our side. Downstream code reads
`profile["languages"]`, not paragraphs to re-parse.

**One module talks to the AI (`llm.py`).** Everything else calls
`llm.ask(Model, system=..., prompt=..., model=...)`. Tests replace that one
function with a fake that records every call, so the suite never spends money
or needs a network connection. Switching to another provider later only
touches this file. The SDK already retries rate limits and server errors.

**Extract once, reuse forever.** A profile is stored with the hash of the
resume text, the prompt version and the model. Running `profile extract`
again with the same resume returns the stored profile without calling the
AI. A new resume, a changed prompt (`PROMPT_VERSION`) or `--force` creates a
new version, and the newest version is the active one. Old versions are kept,
so it's clear which profile a past match was scored against.

**Data minimization.** The profile has no name, email, phone, address or
links, and the prompt tells the model to leave them out. Matching doesn't
need them, so they're never stored.

**Facts, not guesses.** The prompt says to use only what the resume states and
to leave lists empty when unknown. `years_of_experience` counts only full-time
work after graduation, so internships don't make a new grad look "mid-level".

**Preferences live in the file, not the database.** They're things you'll
tweak often (roles, interests, things to avoid, visa sponsorship, free-form
notes). Editing a file in git is easier than re-running a command, and the
matcher reads them fresh each time.

**A stronger model for a one-off call.** Extraction runs once per resume, so it
uses Claude Sonnet 5.5 by default (override with `JOBRADAR_PROFILE_MODEL`).
Matching runs per job and uses a cheaper model (next slice).

**Clear errors.** A missing API key, an image-only (scanned) PDF or a missing
file each give a one-line message and exit code 2. Without the explicit key
check, the SDK would crash with a bare `TypeError`.

## Not done (on purpose)

- **Editing the extracted profile by hand** (export, edit, import). For now,
  corrections go in `notes`. Worth adding if extraction turns out to miss
  things often.
- **OCR for scanned PDFs:** export a text PDF, or pass a `.txt` or `.md` resume.

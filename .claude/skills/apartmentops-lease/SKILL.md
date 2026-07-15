---
name: apartmentops-lease
description: >-
  ApartmentOps stage 5 - lease abstraction, deviation flags, and critical
  dates. The user drops a draft lease PDF into apartmentops/data/leases/;
  this skill converts it locally, extracts every renter-relevant term with
  a page/clause citation (or MISSING/CONFLICT - never guessed), compares
  the lease against the advertised deal in apartmentops/data/verified.json
  to flag deviations (a vanished concession, a surprise fee), and derives
  a critical-dates timeline (renewal notice, concession reversion,
  deposit-return deadline) the user copies into their own calendar. Never
  sends the lease anywhere, never auto-adds a calendar entry, never
  contacts anyone. Use when the user says "review my lease", "check this
  lease against the listing", "when do I need to give notice", "lease
  timeline", or has just dropped a lease PDF into the leases folder.
---

# ApartmentOps: Lease (abstraction, deviations, critical dates)

Inputs: a draft lease PDF the user places at
`apartmentops/data/leases/<unit-slug>/<file>.pdf`, plus (when it exists)
that unit's record in `apartmentops/data/verified.json`. Output:
`apartmentops/data/lease.json`, one entry per unit slug (schema below).
`apartmentops/` is already covered by the repo's blanket `.gitignore`
entry, so `data/leases/` and `data/lease.json` never get staged - no
further gitignore change is needed, but never copy lease content into any
other file that is not already under that ignore.

This skill is pure analysis of a document the user supplied. It is not a
scanner, it does not log in anywhere, and it never contacts a landlord or
broker. Read `../apartmentops/references/lease-fields.md` (the field
dictionary) and `../apartmentops/references/provenance.md` (the FACT /
MISSING / CONFLICT provenance schema this skill extracts against - that
file ships in this repo) before doing extraction work.

## Step 1 - Intake

Confirm the PDF is at `apartmentops/data/leases/<unit-slug>/<file>.pdf`
(create the folder if the user has not yet; ask for the unit slug if it is
ambiguous - reuse the slug `verified.json` already uses for that unit so
the deviation step in Step 3 can join on it).

Convert it to text **locally, never sending the document to any external
service**: `pip install markitdown` and run it against the PDF, preserving
page boundaries so later citations stay page-accurate. If markitdown is
unavailable or fails, any other local PDF-to-text tool is fine as long as
it stays local and you can still cite pages. A scanned, image-only PDF may
convert poorly (garbled text, dropped tables, missing page breaks) - note
that up front; affected fields become `MISSING` with a conversion-quality
note in Step 2, never a guessed value.

## Step 2 - Extract, with provenance

Walk `references/lease-fields.md` field by field. For each field id in the
dictionary, extract using the provenance schema in
`references/provenance.md`:

- **FACT** - the converted text supports it; cite the page and clause
  (and, ideally, a short verbatim quote).
- **MISSING** - the lease is silent on it. This is a normal, honest
  outcome for plenty of fields (e.g. most residential leases say nothing
  about tenant-initiated early termination) - do not treat "the dictionary
  has an entry" as pressure to produce a value.
- **CONFLICT** - two clauses (or the body vs an addendum) disagree; keep
  both citations, do not pick a winner.

Never guess, interpolate, or carry a value over from a similar clause
elsewhere in the document. If conversion quality was poor for a field
(Step 1), record it `MISSING` with a one-line conversion-quality note
rather than inferring from a partial read.

Write the result into `fields` in `apartmentops/data/lease.json` (schema
below), keyed by the field ids from `lease-fields.md`.

## Step 3 - Deviations against the advertised deal

Look up the unit's record in `apartmentops/data/verified.json` by the same
slug. If there is no record, write a single deviations entry noting "No
advertised record on file - nothing to compare" and stop this step; do not
fabricate an advertised value to compare against.

If a record exists, compare field by field using this map (advertised
field in `verified.json` -> lease field id):

| Advertised (`verified.json`) | Lease field id                        | Flag if...                                   |
|-------------------------------|----------------------------------------|-----------------------------------------------|
| `rent_verified` / `rent_gross` | `rent_amount`                         | different amount, or lease is silent          |
| `concession`                   | `concession.free_months`, `concession.position`, `concession_reversion_note` | advertised concession is `MISSING` in the lease, or the reversion terms differ from what the advertised deal implied |
| (no listing field - lease-only) | `fee_amenity`, `fee_admin`, `fee_late`, `fee_application`, `fee_pet` | a fee appears in the lease that the listing never mentioned |

For each comparison, record both sides with their own citation: the
advertised value plus the listing's `unit_deep_link` (or `url`) from
`verified.json`, and the lease value (or its `MISSING`/`CONFLICT` status)
plus its page/clause citation. Two flag levels:

- **red** - money-relevant (rent, concession, any fee).
- **note** - wording-only differences that do not change the dollar
  amount.

A lease field that is `MISSING` compares as "Cannot compare - lease term
MISSING", never as an inferred match or mismatch. If every compared field
matches, record an explicit "Lease matches advertised deal (N fields
compared)" line - silence is not an acceptable way to say "no deviations".

Write the result into `deviations` in `lease.json`. Never message, email,
or otherwise contact the landlord or broker about a deviation - this step
produces a list for the user to raise themselves, citing page and clause.

## Step 4 - Critical dates

Assemble a `fields` object for `scripts/lease_dates.py` from what Step 2
extracted - only include a key when that field is a `FACT` (a `MISSING` or
`CONFLICT` lease term should simply be left out, so the script reports it
under `skipped` rather than you pre-guessing an omission):

```json
{
  "lease_start": "2026-08-01",
  "lease_end": "2027-07-31",
  "renewal_notice_days": 60,
  "concession": {"free_months": 2, "position": "front"},
  "increase_notice_days": 30,
  "deposit_return_days": 30
}
```

**Statutory notice periods are never hardcoded.** If the lease states a
renewal-notice or rent-increase-notice day count as `FACT`, use it
directly. If the lease instead says "as required by applicable law" (or is
silent on the day count while local rent control governs it - Hoboken and
Jersey City both have municipal ordinances), research the specific public
law for the property's jurisdiction during this session (a web search
naming the statute/ordinance and section), and only then add the day count
to `fields`, together with the statute name, section, and URL you found -
that citation belongs in the calendar-entry text in Step 5, not inside
`lease_dates.py`'s input (which only takes a day count). If you do not
research it, omit the field entirely; it will surface in `skipped` with
reason `"statute not researched"` when you write the note, as distinct
from `"lease term MISSING"` for fields the lease itself never states.

Run:

```
python3 ../apartmentops/scripts/lease_dates.py fields.json --today YYYY-MM-DD
```

(or import `derive_dates(fields, today=None)` directly - it is a plain
function, no subprocess needed if you are already in a Python context).
It returns `{"dates": [...], "skipped": [...]}`. Copy `dates` verbatim into
`lease.json`'s `dates` key. For each entry in `skipped`, write a
`dates_skipped` entry with the label and a reason - preserve the script's
`missing` text, and prepend `"statute not researched - "` when that is why
a jurisdiction-sourced field was left out (see above), or leave it as
`"lease term MISSING"` framing when the lease itself never stated the
input.

## Step 5 - Offer calendar entries (text only, never sent)

For every entry in `dates`, offer the user a copyable plain-text calendar
block: title, date, and a one-line description carrying the lease citation
and, where relevant, the statute name/URL from Step 4. For example:

```
Title: Renewal notice deadline - <unit slug>
Date: 2027-06-01
Notes: Lease p. 9, clause 14(a) requires 60 days' notice before lease_end
(2027-07-31). Give notice by certified mail per clause 14(c).
```

The user copies this into their own calendar. **Never add it to any
calendar, never email it, never message it to anyone on the user's
behalf** - this skill produces text, not actions.

## `apartmentops/data/lease.json` schema

One entry per unit slug:

```json
{
  "<unit-slug>": {
    "unit_id": "<unit-slug>",
    "extracted_at": "2026-07-15T14:02:11-04:00",
    "source_pdf": "apartmentops/data/leases/<unit-slug>/<file>.pdf",
    "conversion_note": "free text, e.g. scanned pages 3-5 converted poorly",
    "fields": {
      "<field-id-from-lease-fields.md>": {
        "value": "... or null",
        "status": "FACT | MISSING | CONFLICT",
        "citations": [{"page": 12, "clause": "24(b)", "quote": "optional verbatim text"}]
      }
    },
    "deviations": [
      {
        "field": "concession",
        "advertised": {"value": "...", "source": "unit_deep_link or url from verified.json"},
        "lease": {"value": "... or null", "status": "FACT | MISSING | CONFLICT", "citations": [...]},
        "flag": "red | note"
      }
    ],
    "dates": [
      {"date": "2027-06-01", "label": "Renewal notice deadline",
       "computed_from": "lease_end (2027-07-31) minus renewal_notice_days (60) days",
       "status": "upcoming | past_due"}
    ],
    "dates_skipped": [
      {"label": "Rent-increase notice deadline", "missing": "statute not researched"}
    ]
  }
}
```

`fields` follows the provenance schema in `references/provenance.md`;
`citations` uses page/clause (plus an optional quote) rather than a URL,
since the source is a document, not a webpage. `dates` and `dates_skipped`
are `derive_dates()`'s `dates` and `skipped` output, copied through
(`dates_skipped` adds the statute-vs-lease-silent distinction from Step 4
that the script itself does not know).

## Wrap up

Summarize for the user: fields extracted (counts by FACT/MISSING/CONFLICT),
deviation count by flag level, and the critical-dates list with any
`dates_skipped` reasons. Remind them this is not legal advice - the skill
abstracts and flags, they decide, and for anything ambiguous they should
consult counsel. Offer the calendar-entry text from Step 5 for each date.

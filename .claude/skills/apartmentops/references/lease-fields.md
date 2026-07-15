# ApartmentOps lease field dictionary (renter-side)

This is the field dictionary the `apartmentops-lease` skill extracts a
draft lease against. It is deliberately renter-side, not landlord/CRE-side:
deposit return, concession reversion, and renewal-notice mechanics are
first-class fields because they are the ones a renter needs a deadline for,
not the ones a landlord's counsel needs a clause for.

Every field is extracted using the provenance schema documented in
`references/provenance.md` (that file ships in this repo - read it before
extracting). In short: a field is `FACT` only when you can point at a page
and clause in the converted lease text, `MISSING` when the lease is silent
on it, or `CONFLICT` when two clauses disagree (both cited, no silent
winner). Never a guessed or interpolated value. For a lease specifically,
"source" and "evidence" mean a page number and a clause/section reference
(and, ideally, a short verbatim quote) rather than a URL or screenshot -
adapt the schema's citation shape accordingly.

The field id column is the key each field is written under in
`fields` inside `apartmentops/data/lease.json` (see
`.claude/skills/apartmentops-lease/SKILL.md` for the full file shape). The
ids marked "-> lease_dates.py" are consumed directly by
`scripts/lease_dates.py`'s `derive_dates()` - use those exact ids and
shapes so the date-derivation step in the skill can hand its `fields`
dict straight to the script without translation.

## Rent schedule

- **Field id:** `rent_amount` (plus `lease_start` / `lease_end` -> lease_dates.py)
- **What to look for:** the base monthly rent actually charged, the first
  and last day of the term, and whether rent is billed monthly, or on some
  other schedule (rare, but some commercial-adjacent leases do quarterly).
- **Where it usually appears:** the first page or the "Term and Rent"
  section, often restated in a payment-schedule addendum.
- **Provenance requirement:** FACT needs the dollar figure plus the exact
  term dates, cited by page/clause. A lease that states rent but omits a
  clear start or end date is `CONFLICT` if two sections disagree on the
  term, or `MISSING` for whichever half is absent.

## Escalations

- **Field id:** `escalation_schedule`
- **What to look for:** any contractual rent increase during the term
  (multi-year leases) or at renewal - a fixed dollar amount, a percentage,
  or an index (e.g. CPI-linked). Note whether the escalation is automatic
  or requires landlord notice to take effect.
- **Where it usually appears:** "Rent Adjustments", "Escalation", or buried
  in a renewal-option clause.
- **Provenance requirement:** FACT needs the mechanism and the rate/formula
  cited by clause. A lease with no escalation language for its (single-year)
  term is legitimately `MISSING` - do not infer a "standard" escalation that
  is not written down.

## Concession terms

- **Field id:** `concession.free_months`, `concession.position` (`"front"`
  or `"spread"`) -> lease_dates.py; free-text reversion description as
  `concession_reversion_note`
- **What to look for:** any free rent, credit, or reduced-rate period -
  how many months, whether the free time sits at the front of the term
  ("front-loaded", the usual case and the only one `lease_dates.py` can
  derive a single reversion date for) or is spread across every month's
  invoice ("spread", e.g. "$200/mo credit for 12 months"), and the exact
  reversion language (what rent reverts to, and when).
- **Where it usually appears:** a "Concession", "Rent Credit", or "Special
  Provisions" addendum - frequently a separate signed page, easy to miss.
- **Provenance requirement:** FACT needs the number of months, the
  position (front vs spread), and the reversion rent, each cited. If the
  lease is silent on a concession the listing advertised, that is not a
  `MISSING` field in isolation - it is the canonical deviation case (see
  the deviation-flag step in the skill): record `MISSING` here, and let
  the deviation comparison against `verified.json` surface it as a red
  flag with the listing's citation on the advertised side.

## Security deposit

- **Field id:** `security_deposit_amount`, `deposit_return_days`
  (-> lease_dates.py)
- **What to look for:** the deposit dollar amount, and the number of days
  after move-out (or after `lease_end`) the landlord has to return it or
  provide an itemized deduction list. Some leases cite a statutory number
  ("as required by law") instead of stating a day count - see the
  statutory-research note in the skill before treating that as `MISSING`.
- **Where it usually appears:** "Security Deposit" section, often near the
  rent clause.
- **Provenance requirement:** FACT needs both the amount and the return
  window cited. A lease that states the amount but not a return window is
  `FACT` for the amount and `MISSING` for `deposit_return_days` (unless a
  statute is researched and cited during the session - never hardcoded).

## Sublet and assignment

- **Field id:** `sublet_assignment_terms`
- **What to look for:** whether subletting or assignment is permitted at
  all, whether landlord consent is required (and cannot be "unreasonably
  withheld" or is fully discretionary), any fee for processing a sublet
  request, and any flat prohibition.
- **Where it usually appears:** "Assignment and Subletting" - usually its
  own numbered clause.
- **Provenance requirement:** FACT needs the permission level (prohibited /
  consent-required / permitted) and the clause. Silence on subletting in a
  lease that is otherwise complete is unusual but possible - record
  `MISSING`, do not assume "prohibited by default" without a citation.

## Early termination

- **Field id:** `early_termination_terms`
- **What to look for:** any buyout clause, liquidated-damages formula,
  military/hardship clause, or flat "no early termination" statement, plus
  the notice required to invoke it.
- **Where it usually appears:** "Early Termination", "Buyout", or
  "Default and Remedies".
- **Provenance requirement:** FACT needs the mechanism (fee formula or
  prohibition) and notice terms, cited. Most residential leases are silent
  on a tenant-initiated early-termination right beyond statutory or
  hardship carve-outs - `MISSING` is a normal, honest outcome here.

## Renewal notice mechanics

- **Field id:** `renewal_notice_days`, `renewal_notice_method`
  (`renewal_notice_days` -> lease_dates.py)
- **What to look for:** how many days before `lease_end` either party must
  give notice of non-renewal or intent to renew, and the required method
  (certified mail, a specific email address, a portal, hand delivery).
  Getting the method wrong can void a timely notice, so extract it even
  though `lease_dates.py` only consumes the day count.
- **Where it usually appears:** "Renewal", "Notice to Vacate", or a
  holdover clause that implies the same window.
- **Provenance requirement:** FACT needs the day count and the method, each
  cited. If the lease says "as required by applicable law" instead of a
  number, treat the day count as `MISSING` here and follow the skill's
  statutory-research step (cite the specific public law or mark UNKNOWN -
  never hardcode a number this dictionary or the skill invented).

## Fee clauses

- **Field id:** `fee_amenity`, `fee_admin`, `fee_late`, `fee_application`,
  `fee_pet` (use whichever subset the lease actually contains)
- **What to look for:** every recurring or one-time fee named in the
  lease - amenity/facility fee, administrative or move-in fee, late-payment
  fee (flat or percentage, and the grace period before it applies),
  application fee, pet fee or pet rent. Cross-reference against what the
  listing advertised (or omitted) in the deviation step.
- **Where it usually appears:** scattered - a fee schedule addendum if one
  exists, otherwise embedded in the relevant clause (late fees inside the
  rent clause, pet fees inside a pet addendum).
- **Provenance requirement:** each fee is its own FACT with amount and
  clause, or `MISSING` if the lease does not mention that fee type at all.
  A fee schedule addendum that contradicts a fee mentioned in the body of
  the lease is `CONFLICT` - keep both citations.

## Insurance requirements

- **Field id:** `insurance_requirement`
- **What to look for:** whether renter's/liability insurance is required,
  any minimum coverage amount, and whether proof must be provided before
  move-in or maintained for the whole term.
- **Where it usually appears:** "Insurance" or "Tenant Obligations".
- **Provenance requirement:** FACT needs the requirement and minimum
  coverage (if stated) cited. Many leases only "recommend" insurance
  without requiring it - extract that distinction faithfully rather than
  rounding it up to "required".

## Honest unknowns

Scanned image-only PDFs frequently convert poorly through markitdown -
garbled text, dropped tables, missing page breaks. When conversion quality
is in doubt for a field, extract what the converted text actually supports
and mark the rest `MISSING` with a short conversion-quality note attached
(see the skill's extraction step) - never infer a value to compensate for a
bad conversion.

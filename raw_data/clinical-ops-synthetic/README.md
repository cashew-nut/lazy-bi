# Synthetic Clinical Operations Dataset

Fully synthetic (seeded, reproducible) — 20 studies, "as of" 2026-07-22.
No real trials, sponsors, sites, or people. Provided as both `.parquet`
(recommended for Polars/lazy-bi) and `.csv` (for quick inspection).

## Tables & grain

| Table              | Grain                        | Rows |
|---------------------|-------------------------------|------|
| `studies`           | 1 row per study                | 20   |
| `sites`              | 1 row per site per study        | 400  |
| `milestones`         | 1 row per study per milestone type | 200  |
| `recruitment`        | 1 row per site per month        | ~3.3k |

## Relationships
- `sites.study_id` → `studies.study_id`
- `milestones.study_id` → `studies.study_id`
- `recruitment.study_id` / `recruitment.site_id` → `studies` / `sites`

`sites.actual_enrollment` = sum of `recruitment.patients_enrolled` for that site.
`studies.actual_enrollment` = sum of site-level `actual_enrollment` for that study.

## studies
`study_id, study_title, sponsor, indication, phase, status,
planned_start_date, actual_start_date,
planned_primary_completion_date, actual_primary_completion_date,
planned_study_completion_date, actual_study_completion_date,
target_enrollment, actual_enrollment, num_sites, num_countries`

`status` ∈ {Not yet recruiting, Recruiting, Active not recruiting, Completed, Terminated, Suspended}

## sites
`site_id, study_id, site_name, country, principal_investigator,
planned_activation_date, actual_activation_date,
target_enrollment, actual_enrollment, site_status`

`site_status` ∈ {Identified, Activated, Enrolling, Enrollment Complete, Enrollment Closed}
(`actual_activation_date` is null for sites not yet activated.)

## milestones (study-level program milestones)
`milestone_id, study_id, milestone_type, planned_date, actual_date, status, variance_days`

`milestone_type` sequence: Protocol Approved → First IRB/EC Approval → First Site
Activated → First Patient In → 25% Enrollment → 50% Enrollment → Last Patient In →
Database Lock → Last Patient Out → Clinical Study Report Finalized.
`status` ∈ {Completed, Pending, Delayed}; `variance_days` = actual − planned (null if not yet occurred).

## recruitment (monthly site enrollment activity)
`study_id, site_id, month, patients_screened, patients_enrolled,
screen_failures, cumulative_enrolled`

One row per site per active month — good for recruitment-curve charts
(actual vs. target, cumulative vs. monthly velocity).

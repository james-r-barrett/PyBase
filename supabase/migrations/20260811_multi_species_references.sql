-- Multi-species references: a paper can describe several species, each needing
-- a separate submission. Previously mark_queue_covered() closed a literature_queue
-- row to 'covered' the moment ANY submission matched its DOI, so a paper covering
-- 3 species vanished into the completed pile after just 1 was logged.
--
-- Fix: submitters now say up front whether the reference covers more than one
-- species. That answer routes a DOI match to 'in_progress' instead of an
-- automatic 'covered'; a curator closes it out manually once satisfied.

-- 1. New required-at-submission-time flag.
alter table submissions
  add column if not exists reference_multi_species boolean not null default false;

-- 2. Route DOI matches based on that flag instead of always auto-closing.
--    - single species: same behaviour as before (only closes rows still at to_review)
--    - multi species: routes to in_progress; can also pull a row back out of
--      'covered' if an earlier submission wrongly called it single-species
create or replace function public.mark_queue_covered()
returns trigger
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  norm_doi text;
begin
  if new.doi is null or new.doi = '' then
    return new;
  end if;

  norm_doi := lower(regexp_replace(new.doi, '^https?://(dx\.)?doi\.org/', '', 'i'));

  if new.reference_multi_species then
    update literature_queue
    set status = 'in_progress'
    where status in ('to_review', 'covered')
      and doi is not null
      and lower(regexp_replace(doi, '^https?://(dx\.)?doi\.org/', '', 'i')) = norm_doi;
  else
    update literature_queue
    set status = 'covered'
    where status = 'to_review'
      and doi is not null
      and lower(regexp_replace(doi, '^https?://(dx\.)?doi\.org/', '', 'i')) = norm_doi;
  end if;

  return new;
end;
$function$;

-- 3. Manual close-out for in_progress (or to_review) rows, mirroring the shape
--    of the existing mark_literature_not_relevant / mark_literature_to_review.
create or replace function public.mark_literature_covered(item_id uuid)
returns void
language sql
security definer
set search_path to 'public'
as $function$
  update literature_queue
  set status = 'covered'
  where id = item_id and status in ('to_review', 'in_progress');
$function$;

-- 4. Read-only view of already-approved species per DOI, so the to-do page can
--    show what's logged against a reference without granting broader SELECT
--    access to the submissions table (which holds submitter emails etc).
create or replace view public.approved_species_by_doi as
select
  lower(regexp_replace(doi, '^https?://(dx\.)?doi\.org/', '', 'i')) as norm_doi,
  species_name
from submissions
where status = 'approved'
  and doi is not null
  and doi <> '';

grant select on public.approved_species_by_doi to anon, authenticated;

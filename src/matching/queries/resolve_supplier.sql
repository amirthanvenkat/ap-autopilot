-- Which supplier sent this invoice?
--
-- Spec 02 section 4, with README challenge 6.4 adopted. Returns exactly one
-- row: the supplier, how it was chosen and its score, or a null supplier
-- and the reason no supplier could be chosen safely. A wrongly resolved
-- supplier pays the wrong bank account, so every doubtful case resolves to
-- nothing and goes to a reviewer.
--
-- Tiers, tried in order. The first to reach a verdict wins:
--   1. Tax ID, corroborated by the name.
--   2. Exact normalised legal or trading name.
--   3. Trigram similarity, above the threshold and clear of the runner-up.
--
-- Scores are rounded to four places, the precision of
-- invoices.supplier_resolution_score. similarity() returns a real, and a
-- real 0.82 is 0.8199999..., which would fail a threshold of exactly 0.82.
--
-- The caller first sets pg_trgm.similarity_threshold for the transaction,
-- just below the lowest score that can still matter (see suppliers.py).
-- That lets the % operator use the trigram index on legal names without
-- hiding a runner-up.
--
-- Parameters: :tax_id, :name, :trigram_threshold, :ambiguity_margin,
-- :tax_id_name_floor.

with input as (
    select normalise_tax_id(cast(:tax_id as text))        as tax_id,
           normalise_supplier_name(cast(:name as text))   as name,
           cast(:trigram_threshold as numeric)            as threshold,
           cast(:ambiguity_margin as numeric)             as margin,
           cast(:tax_id_name_floor as numeric)            as floor
),

-- Every name a supplier trades under, normalised.
supplier_names as (
    select s.supplier_id, normalise_supplier_name(s.legal_name) as name
      from suppliers s
    union
    select s.supplier_id, n.name
      from suppliers s
     cross join unnest(normalise_supplier_names(s.trading_names)) as n (name)
),

-- Tier 1. At most one row: the tax ID index is unique in normalised form.
-- The name score is the corroboration. A null extracted name scores zero,
-- because an uncorroborated tax ID is exactly what 6.4 refuses.
tax_id_hit as (
    select s.supplier_id,
           s.is_active,
           coalesce(
               (select max(round(similarity(sn.name, i.name)::numeric, 4))
                  from supplier_names sn
                 where sn.supplier_id = s.supplier_id),
               0
           ) as name_score
      from suppliers s
     cross join input i
     where normalise_tax_id(s.tax_id) = i.tax_id
),

-- Tier 2.
exact as (
    select distinct sn.supplier_id
      from supplier_names sn
     cross join input i
     where sn.name = i.name
),

-- Tier 3. Legal names go through the % operator and its index. Trading
-- names cannot be indexed by pg_trgm, so they are scanned.
trigram as (
    select c.supplier_id, max(c.score) as score
      from (
        select s.supplier_id,
               round(similarity(normalise_supplier_name(s.legal_name), i.name)::numeric, 4)
                   as score
          from suppliers s
         cross join input i
         where normalise_supplier_name(s.legal_name) % i.name
        union all
        select s.supplier_id,
               round(similarity(n.name, i.name)::numeric, 4)
          from suppliers s
         cross join input i
         cross join unnest(normalise_supplier_names(s.trading_names)) as n (name)
         where n.name % i.name
      ) c
     group by c.supplier_id
),

ranked as (
    select supplier_id,
           score,
           row_number() over (order by score desc, supplier_id) as position
      from trigram
),

verdicts as (
    select 1 as priority,
           t.supplier_id as candidate_id,
           t.is_active and t.name_score >= i.floor as accepted,
           'TAX_ID' as method,
           t.name_score as score,
           case
               when not t.is_active then
                   format('tax ID matches inactive supplier %s', t.supplier_id)
               when t.name_score < i.floor then
                   format('tax ID matches supplier %s, but the name scores %s '
                          'against it, below the corroboration floor of %s',
                          t.supplier_id, t.name_score, i.floor)
           end as reason
      from tax_id_hit t
     cross join input i

    union all

    select 2,
           min(e.supplier_id),
           count(*) = 1,
           'NAME_EXACT',
           1.0000,
           case when count(*) > 1 then
               format('name matches %s suppliers exactly: %s',
                      count(*), string_agg(e.supplier_id, ', ' order by e.supplier_id))
           end
      from exact e
    having count(*) > 0

    union all

    select 3,
           best.supplier_id,
           best.score >= i.threshold
               and (runner_up.score is null
                    or best.score - runner_up.score > i.margin),
           'NAME_TRIGRAM',
           best.score,
           case
               when best.score < i.threshold then
                   format('closest supplier %s scores %s, below the threshold of %s',
                          best.supplier_id, best.score, i.threshold)
               when best.score - runner_up.score <= i.margin then
                   format('suppliers %s and %s score %s and %s, within the '
                          'ambiguity margin of %s',
                          best.supplier_id, runner_up.supplier_id,
                          best.score, runner_up.score, i.margin)
           end
      from ranked best
     cross join input i
      left join ranked runner_up on runner_up.position = 2
     where best.position = 1

    union all

    select 4, null, false, null, null,
           'no supplier matches the tax ID or the name'
),

chosen as (
    select * from verdicts order by priority limit 1
),

-- Checks a name match must also pass. A tax ID hit has already been
-- checked above.
checked as (
    select c.*,
           s.is_active,
           s.tax_id as supplier_tax_id,
           case
               when not c.accepted or c.method = 'TAX_ID' then c.reason
               when not s.is_active then
                   format('name matches inactive supplier %s', s.supplier_id)
               -- The invoice shows a tax ID that belongs to nobody, and the
               -- supplier the name found is registered under another one.
               -- Conflicting evidence: refuse rather than pick a side.
               when i.tax_id is not null
                    and s.tax_id is not null
                    and normalise_tax_id(s.tax_id) <> i.tax_id then
                   format('name matches supplier %s, but the invoice tax ID %s '
                          'differs from its registered %s',
                          s.supplier_id, i.tax_id, normalise_tax_id(s.tax_id))
           end as refusal
      from chosen c
     cross join input i
      left join suppliers s on s.supplier_id = c.candidate_id
)

select case when refusal is null then candidate_id end as supplier_id,
       case when refusal is null then method end        as method,
       case when refusal is null then score end         as score,
       refusal                                          as reason
  from checked

-- Derive the exceptions that follow from an invoice's recorded facts, and
-- make the exceptions table agree with them.
--
-- README 5.2 and 5.6. Canonicalisation records facts; this statement turns
-- them into exceptions on every run, so an exception is never resolved
-- just because a later run did not write it again.
--
-- Raised here: SUPPLIER_UNRESOLVED, NO_PO_REFERENCE, PO_NOT_FOUND,
-- DUPLICATE_EXACT and LOW_CONFIDENCE. The line match adds its own rows to
-- `raised` when it is built.
--
-- Rows are upserted on (invoice_id, exception_key), so exception_id
-- survives a re-run (README 6.8):
--   * a row raised again is refreshed, and reopened if the matcher had
--     resolved it;
--   * a matcher row still OPEN but no longer raised is resolved by the
--     matcher;
--   * WAIVED rows and rows a reviewer resolved are left alone. Whether a
--     reviewer's resolution should reopen is README open question 7.2.
--
-- Parameters:
--   :invoice_id
--   :thresholds   jsonb, header field -> confidence threshold
--   :required     text[], header fields that must carry a value
--   :severities   jsonb, exception type -> BLOCK or WARN

with invoice as (
    select * from invoices where invoice_id = :invoice_id
),

thresholds as (
    select key as field, cast(value as numeric) as threshold
      from jsonb_each_text(cast(:thresholds as jsonb))
),

raised as (
    select 'invoice:SUPPLIER_UNRESOLVED' as exception_key,
           'SUPPLIER_UNRESOLVED'         as exception_type,
           cast(null as text)            as field_path,
           cast(null as text)            as expected_value,
           cast(null as text)            as actual_value,
           i.supplier_resolution_note    as detail
      from invoice i
     where i.supplier_id is null

    union all

    select 'invoice:NO_PO_REFERENCE',
           'NO_PO_REFERENCE',
           '/po_number',
           null,
           i.po_number,
           coalesce(i.po_resolution_note, 'the invoice quotes no purchase order')
      from invoice i
     where normalise_reference(i.po_number) is null

    union all

    select 'invoice:PO_NOT_FOUND',
           'PO_NOT_FOUND',
           '/po_number',
           null,
           i.po_number,
           i.po_resolution_note
      from invoice i
     where normalise_reference(i.po_number) is not null
       and i.po_id is null

    union all

    select format('duplicate:%s:DUPLICATE_EXACT', i.duplicate_of_invoice_id),
           'DUPLICATE_EXACT',
           '/invoice_number',
           i.duplicate_of_invoice_id,
           i.invoice_number,
           format('supplier %s already invoiced number %s as invoice %s',
                  i.supplier_id, i.invoice_number, i.duplicate_of_invoice_id)
      from invoice i
     where i.duplicate_of_invoice_id is not null

    union all

    -- Low confidence, or a required field reported with no value. A field
    -- that was never reported has no row, so it raises nothing here.
    select format('field:%s:LOW_CONFIDENCE', f.field_path),
           'LOW_CONFIDENCE',
           f.field_path,
           cast(t.threshold as text),
           cast(f.confidence as text),
           case
               when f.field_value is null then
                   format('%s was reported but could not be read', t.field)
               else
                   format('%s read as %L with confidence %s, below the threshold of %s',
                          t.field, f.field_value,
                          coalesce(cast(f.confidence as text), 'not reported'),
                          t.threshold)
           end
      from invoice i
      join extraction_fields f on f.extraction_id = i.extraction_id
      join thresholds t on f.field_path = '/' || t.field
     where (f.field_value is not null
            and (f.confidence is null or f.confidence < t.threshold))
        or (f.field_value is null
            and t.field = any(cast(:required as text[])))
),

classified as (
    select r.*,
           (cast(:severities as jsonb) ->> r.exception_type) as severity
      from raised r
),

upserted as (
    insert into exceptions (
        invoice_id, exception_key, exception_type, severity, source,
        field_path, expected_value, actual_value, detail
    )
    select :invoice_id, c.exception_key, c.exception_type, c.severity, 'MATCHER',
           c.field_path, c.expected_value, c.actual_value, c.detail
      from classified c
    on conflict (invoice_id, exception_key) do update
       set severity       = excluded.severity,
           field_path     = excluded.field_path,
           expected_value = excluded.expected_value,
           actual_value   = excluded.actual_value,
           detail         = excluded.detail,
           status         = 'OPEN',
           resolved_by    = null,
           resolved_at    = null,
           updated_at     = now()
     where exceptions.source = 'MATCHER'
       and (exceptions.status = 'OPEN' or exceptions.resolved_by = 'MATCHER')
    returning exception_id
),

closed as (
    update exceptions e
       set status = 'RESOLVED',
           resolved_by = 'MATCHER',
           resolved_at = now(),
           updated_at = now()
     where e.invoice_id = :invoice_id
       and e.source = 'MATCHER'
       and e.status = 'OPEN'
       and e.exception_key not in (select exception_key from raised)
    returning exception_id
),

-- Every data modifying CTE reads the snapshot taken before the statement,
-- so whether the invoice is blocked is worked out from what was raised and
-- the rows as they stood. A raised BLOCK counts unless a person has already
-- waived or resolved that exact exception.
blocking as (
    select exists (
        select 1
          from classified c
          left join exceptions e
                 on e.invoice_id = :invoice_id
                and e.exception_key = c.exception_key
         where c.severity = 'BLOCK'
           and (e.exception_id is null
                or e.status = 'OPEN'
                or e.resolved_by = 'MATCHER')
    ) as blocked
)

-- Until the line match exists, an invoice with no blocking fact stays
-- PENDING: it has not yet been shown to match. POSTED and REJECTED are
-- final and never reach this statement.
update invoices i
   set match_status = case when b.blocked then 'EXCEPTION' else 'PENDING' end
  from blocking b
 where i.invoice_id = :invoice_id
returning i.match_status,
          (select count(*) from upserted) as upserted,
          (select count(*) from closed)   as closed

-- Create the canonical lines for one invoice, in one statement.
--
-- extraction_fields holds the validated, normalised value of every field,
-- one row each, keyed by a JSON Pointer such as /line_items/3/unit_price.
-- Grouping on the array index pivots those rows back into lines. A field
-- the extractor never reported has no row and so becomes null.
--
-- Quantities can be extracted to six places. invoice_lines.quantity holds
-- four, so the cast rounds; the unrounded value stays in extraction_fields.
--
-- Line ids are derived from the invoice and line number, so a replay
-- conflicts rather than duplicating.

with line_fields as (
    select cast(m[1] as int) + 1 as line_number,
           m[2]                  as field,
           f.field_value
      from extraction_fields f
     cross join lateral regexp_match(f.field_path, '^/line_items/(\d+)/([a-z_]+)$') as m
     where f.extraction_id = :extraction_id
       and m is not null
)
insert into invoice_lines (
    invoice_line_id, invoice_id, line_number,
    description, sku, quantity, unit_price, line_total
)
select format('%s:%s', cast(:invoice_id as text), line_number),
       cast(:invoice_id as text),
       line_number,
       max(field_value) filter (where field = 'description'),
       max(field_value) filter (where field = 'sku'),
       cast(max(field_value) filter (where field = 'quantity') as numeric(18,4)),
       cast(max(field_value) filter (where field = 'unit_price') as numeric(18,4)),
       cast(max(field_value) filter (where field = 'line_total') as numeric(18,4))
  from line_fields
 group by line_number
on conflict (invoice_id, line_number) do nothing

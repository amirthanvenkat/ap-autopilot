-- Which purchase order does this invoice quote, and may it be billed?
--
-- README 5.2. PO numbers are compared ignoring case and punctuation, so
-- "PO 2026-0101" finds "PO-2026-0101". The normalised unique index makes
-- that find at most one.
--
-- Returns one row. po_id is set only for an OPEN purchase order that
-- belongs to the resolved supplier. Otherwise note says why, and becomes
-- the PO_NOT_FOUND detail. When the supplier is unresolved, an open PO is
-- still recorded: the invoice is already blocked, and the PO is never used
-- to guess the supplier.
--
-- Parameters: :po_number, :supplier_id.

with input as (
    select cast(:po_number as text)                         as quoted,
           normalise_reference(cast(:po_number as text))    as reference,
           cast(:supplier_id as text)                       as supplier_id
),

found as (
    select po.po_id, po.po_number, po.status, po.supplier_id
      from purchase_orders po
      join input i on normalise_reference(po.po_number) = i.reference
)

select case
           when f.status = 'OPEN'
                and (i.supplier_id is null or f.supplier_id = i.supplier_id)
           then f.po_id
       end as po_id,
       case
           -- Nothing quoted: NO_PO_REFERENCE, which needs no explanation.
           when i.quoted is null then null
           when i.reference is null then
               format('the quoted PO reference %L has no letters or digits', i.quoted)
           when f.po_id is null then
               format('no purchase order matches %L', i.quoted)
           when f.status <> 'OPEN' then
               format('purchase order %s is %s', f.po_number, lower(f.status))
           when i.supplier_id is not null and f.supplier_id <> i.supplier_id then
               format('purchase order %s belongs to supplier %s, not %s',
                      f.po_number, f.supplier_id, i.supplier_id)
       end as note
  from input i
  left join found f on true

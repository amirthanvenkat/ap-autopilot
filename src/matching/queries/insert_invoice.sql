-- Create the canonical invoice for one extraction.
--
-- extraction_results is what the model said; invoices is what the system
-- believes. Header values are copied as extracted. The facts resolved in
-- Python (supplier, PO, duplicate) arrive as parameters.
--
-- A replay conflicts on the extraction and returns no row, so the caller
-- can tell a new invoice from an existing one.

insert into invoices (
    invoice_id, extraction_id,
    supplier_id, supplier_resolution_method, supplier_resolution_score,
    supplier_resolution_note,
    po_number, po_id, po_resolution_note,
    invoice_number, invoice_date, due_date, currency,
    net_amount, tax_amount, total_amount,
    duplicate_of_invoice_id
)
select cast(:invoice_id as text),
       r.extraction_id,
       cast(:supplier_id as text),
       cast(:supplier_method as text),
       cast(:supplier_score as numeric),
       cast(:supplier_note as text),
       r.po_number,
       cast(:po_id as text),
       cast(:po_note as text),
       r.invoice_number,
       r.invoice_date,
       r.due_date,
       r.currency,
       r.net_amount,
       r.tax_amount,
       r.total_amount,
       cast(:duplicate_of as text)
  from extraction_results r
 where r.extraction_id = :extraction_id
on conflict (extraction_id) do nothing
returning invoice_id

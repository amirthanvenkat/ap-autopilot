# Schema and analytical queries

TODO: written by the repository owner. CLAUDE.md section 8 reserves SQL
commentary for a human author, and section 6 says the analytical queries are
part of the portfolio and are written to be read.

To cover once spec 02 lands:

- Schema diagram for the tables created in `sql/schema/versions/0001`.
- Why there are two deduplication layers, and which failure each one
  catches.
- Why `extraction_fields` is one row per field with a JSON Pointer path.
- Annotated queries under `sql/analysis/`, one file per question.

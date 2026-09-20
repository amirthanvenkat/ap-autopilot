# Spec 01 notes

TODO: written by the repository owner. Spec 01 section 10 lists this file as
"recording what went wrong, written by me".

Decisions and surprises from the build, left here as prompts rather than
prose:

- Section 4 asked for 200 in all cases and for transient failures to reach
  the dead letter topic. Those are incompatible. 200 now means durably
  accepted, and the failure classification moved to the worker.
- Section 7 put work after the response. Cloud Run throttles CPU once a
  response returns, so that work can be frozen or lost. It became an outbox.
- `extraction_fields` had no unique constraint. A replay that split the
  writes would have doubled every per field analytic without erroring.
- A Document AI batch writes several output objects, so the completion
  endpoint receives several distinct messages for one job.
- A failed operation writes no output at all, so no completion event ever
  fires and the job sits RUNNING until something sweeps it.
- `users.watch` lapses after seven days without announcing it.
- Typing money as a JSON number would have put a float between the
  extractor and `numeric(18,4)`.

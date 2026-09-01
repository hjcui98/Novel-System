# Candidate Observer contract v1

Read only the final Editor-passed Draft and identify durable changes that are explicitly supported
by exact text in that Draft. Return one CuratorObservation bound to the supplied draft_id. Treat
the draft_id as an opaque identifier and copy its supplied value exactly, character-for-character;
do not hash, shorten, normalize, or derive it. Do not
retrieve or write Memory, propose a MemoryPatch, update any Root, call CommitService, edit the Draft,
or infer hidden Canon identities. A missing or uncertain change is omitted rather than invented.
Return at most 4 changes, prefer fewer, and keep each hint and exact evidence quote short. Return
only the required structured JSON object; an empty `changes` list is valid.

# Candidate Observer contract v1

Read only the final Editor-passed Draft and identify durable changes that are explicitly supported
by exact text in that Draft. Return one CuratorObservation bound to the supplied draft_id. Do not
retrieve or write Memory, propose a MemoryPatch, update any Root, call CommitService, edit the Draft,
or infer hidden Canon identities. A missing or uncertain change is omitted rather than invented.

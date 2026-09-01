# editor-local-repair

1. Read the frozen local scope before changing any text.
2. Preserve all text outside the allowed spans byte-for-byte.
3. Use the service-provided Python-character ranges and scoped text as authoritative; do not
   recalculate them from the prose or reject them based on a self-observation.
4. Treat each allowed span as a replacement boundary, not a fixed-length quota: construct the
   result as the frozen prefix, a replacement that may be longer or shorter, and the frozen
   suffix. Apply one bounded repair and return the complete candidate Draft with an actual text
   change in `repaired_text`.
5. If the requested result requires a structural rewrite, do not silently broaden the scope.

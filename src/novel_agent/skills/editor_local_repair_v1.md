# editor-local-repair

1. Read the frozen local scope before changing any text.
2. Preserve all text outside the allowed spans byte-for-byte.
3. Apply one bounded repair and return the complete candidate Draft.
4. If the requested result requires a structural rewrite, do not silently broaden the scope.

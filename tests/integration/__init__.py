"""Integration tests that simulate external consumers of ollarma.

Tests in this directory verify the public surfaces documented in
``docs/OLLARMA_SUBSTRATE_CONTRACT.md``. Each test imports ollarma the way a
sibling project (Overwatch, Antigence, gettingsciencedone, ...) would: from
the top-level module surface, with no reach into private helpers beyond the
sanctioned ``gateway_admission._KEYCHAIN_LOOKUP`` test seam.

When the contract doc drifts from the code, the tests in this directory
fail — the doc's worked example is re-executed verbatim here.
"""

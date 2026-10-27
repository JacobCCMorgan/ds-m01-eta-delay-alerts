# M2 data milestone scope

Retain the full 1,800-trip January–September 2026 synthetic design; use the original seed/namespace and frozen feature/split definitions. M2 has a new actual batch receipt and pipeline version. The 120-trip M1 business rows, snapshots and labels remain exact subsets; historical pools intentionally grow.

M2 instantiates 28 canonical field schemas, populates the 17 data-stage tables, and leaves 11 future-stage output tables empty. Empty schema creation is not completion of their model/alert requirements. Original 110 requirements are retained; M2 does not assert all are satisfied.

No optional weather forecasts, commercial routing, extra historical warmup data, fitted models or operating-performance claims are introduced. December 2025 valid-from support metadata defines fictional sites/assignments; observed trips remain January–September 2026 with the agreed October 15 extraction cutoff. The optional warmup month is not needed for the first implementation; early snapshots can be explicitly unsupported.

Test cohorts are inspected for validity and support only, not model choice. Training/calibration/test dates, heldout IDs, 12-hour horizon and target cutoff remain unchanged. Sparse heldout cohorts and no-history training snapshots must be carried into M3 evaluation and fallback policy.

Native PostgreSQL verification remains a separate unresolved service check. Local HTTP receipt verification covers telemetry transport and replay parity; it does not prove PostgreSQL integration or production deployment.

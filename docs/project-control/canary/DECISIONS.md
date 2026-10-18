# AOS Disposable Canary Decisions

## Authority: AUTHORITY-CANARY-01

### Authorized scope
- ROUTINE NON-PROD STANDING AUTHORITY for aos-disposable-canary workspace.
- Repair percentage discount calculation defect in src/discount.py.
- Repair tax calculation defect in src/tax.py.
- Execute verification tests test_discount.py and test_tax.py.
- Commit passing defect repairs to the isolated repository.

### Explicitly not authorized
- Production mutation (PRODUCTION=NO_GO).
- Destructive operations.
- Secret exposure.
- Paid provider activation.
- LARI or UI-V2 workspace modification.

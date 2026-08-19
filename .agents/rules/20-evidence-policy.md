# Evidence Policy Rule

Claims without verifiable evidence are insufficient for technical gate progression.

## Evidence Directives
1. **Evidence Outranks Claims**: Narrative statements (e.g., "all tests passed") are `E0_CLAIM` and cannot close a technical gate.
2. **Deterministic Verification Required**: Gates require reproducible, machine-verifiable proof (e.g., E2 test execution, schema validation) matching the target gate's configured minimum level.
3. **Exact Revision & Environment Pinning**: Evidence records must reference exact commit SHAs, environment characteristics, timestamps, tool versions, and limitations.
4. **Planner/Executor/Verifier Separation**: Self-attestation by an executor does not constitute independent verification.
5. **No Falsification or Reshaping**: Never fabricate test runs, browser logs, or alter historical evidence records to fit new constraints.
6. **Contradictions Reopen Gates**: If new evidence contradicts previously accepted evidence, halt execution immediately and enter HOLD.
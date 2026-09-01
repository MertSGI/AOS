/**
 * AOS6 Controlled Pilot Driver Script (Node.js ESM)
 * Runs inside the sealed target container (node:22-bookworm-slim).
 * Executes static QA check (P1) and tests the real ht-ai-chat provider policy module (P2/P3) with deterministic mocks.
 */

import { pathToFileURL } from 'url';
import { join } from 'path';
import { execFileSync } from 'child_process';

// Global fetch poison to prevent any unhandled real network calls
const realNetworkAttempts = { count: 0 };
globalThis.fetch = async (...args) => {
  realNetworkAttempts.count += 1;
  throw new Error("REAL_PROVIDER_NETWORK_PATH_FORBIDDEN");
};

async function runDriver() {
  const workspaceDir = process.env.DISPOSABLE_WORKSPACE_DIR || '/workspace';
  console.log(`[AOS6 Driver] Running inside sealed container at: ${workspaceDir}`);

  let productStaticQaAttemptCount = 0;
  let productStaticQaResult = "NOT_RUN";

  // STEP P1: Product Static / Executable Contract Execution
  try {
    console.log('[AOS6 Driver] Step P1: Executing product static QA script...');
    productStaticQaAttemptCount = 1;

    // Execute scripts/test-health-tourism-slice3-lead-ops-ai-assist.mjs using node --import tsx
    const qaScriptRelative = join('scripts', 'test-health-tourism-slice3-lead-ops-ai-assist.mjs');
    execFileSync('node', ['--import', 'tsx', qaScriptRelative], {
      cwd: workspaceDir,
      stdio: 'inherit',
      env: { ...process.env, NODE_ENV: 'test' }
    });
    productStaticQaResult = "PASS";
  } catch (err) {
    console.error('[AOS6 Driver] Step P1 static QA execution failed:', err);
    emitResult({
      product_static_qa_attempt_count: productStaticQaAttemptCount,
      product_static_qa_result: "FAIL",
      policy_module_boot_result: "NOT_RUN",
      unsafe_grounding_result: "NOT_RUN",
      safe_grounding_result: "NOT_RUN",
      localization_result: "NOT_RUN",
      no_key_provider_result: "NOT_RUN",
      mock_provider_success_result: "NOT_RUN",
      mock_provider_failure_result: "NOT_RUN",
      mock_provider_call_count: 0,
      real_provider_network_call_count: realNetworkAttempts.count,
      bounded_workflow_result: "FAIL",
      error: "STEP_P1_STATIC_QA_FAILED"
    });
    process.exit(1);
  }

  // STEP P2: Load Real Policy Module
  console.log('[AOS6 Driver] Step P2: Loading real provider-policy module...');
  const policyModulePath = join(workspaceDir, 'supabase', 'functions', 'ht-ai-chat', 'provider-policy.ts');
  const policyUrl = pathToFileURL(policyModulePath).href;

  let policy;
  let policyModuleBootResult = "FAIL";
  try {
    policy = await import(policyUrl);
    policyModuleBootResult = "PASS";
  } catch (err) {
    console.error('[AOS6 Driver] Failed to import policy module:', err);
    emitResult({
      product_static_qa_attempt_count: productStaticQaAttemptCount,
      product_static_qa_result: productStaticQaResult,
      policy_module_boot_result: "FAIL",
      unsafe_grounding_result: "NOT_RUN",
      safe_grounding_result: "NOT_RUN",
      localization_result: "NOT_RUN",
      no_key_provider_result: "NOT_RUN",
      mock_provider_success_result: "NOT_RUN",
      mock_provider_failure_result: "NOT_RUN",
      mock_provider_call_count: 0,
      real_provider_network_call_count: realNetworkAttempts.count,
      bounded_workflow_result: "FAIL",
      error: "STEP_P2_POLICY_BOOT_FAILED"
    });
    process.exit(1);
  }

  const {
    isProviderReplyGrounded,
    buildGroundedReplacementResponse,
    executeProviderCall
  } = policy;

  if (typeof isProviderReplyGrounded !== 'function' ||
      typeof buildGroundedReplacementResponse !== 'function' ||
      typeof executeProviderCall !== 'function') {
    console.error('[AOS6 Driver] Required policy functions missing!');
    emitResult({
      product_static_qa_attempt_count: productStaticQaAttemptCount,
      product_static_qa_result: productStaticQaResult,
      policy_module_boot_result: "FAIL",
      unsafe_grounding_result: "NOT_RUN",
      safe_grounding_result: "NOT_RUN",
      localization_result: "NOT_RUN",
      no_key_provider_result: "NOT_RUN",
      mock_provider_success_result: "NOT_RUN",
      mock_provider_failure_result: "NOT_RUN",
      mock_provider_call_count: 0,
      real_provider_network_call_count: realNetworkAttempts.count,
      bounded_workflow_result: "FAIL",
      error: "STEP_P2_POLICY_API_INCOMPLETE"
    });
    process.exit(1);
  }

  console.log('[AOS6 Driver] Step P3: Executing grounded policy matrix...');

  let unsafeGroundingResult = "FAIL";
  let safeGroundingResult = "FAIL";
  let localizationResult = "FAIL";
  let noKeyProviderResult = "FAIL";
  let mockProviderSuccessResult = "FAIL";
  let mockProviderFailureResult = "FAIL";
  let mockProviderCallCount = 0;

  // A. Unsafe operational/provider promise rejected
  const unsafeReply = "Our team will contact you by email.";
  const unsafeCheck = isProviderReplyGrounded(unsafeReply);
  if (unsafeCheck === false) {
    unsafeGroundingResult = "PASS";
  } else {
    console.error('[AOS6 Driver] Unsafe grounding check failed! Reply was marked grounded when it should be ungrounded.');
  }

  // B. Safe coordinator-request language accepted
  const safeReply = "I can summarize your inquiry and record your language preference.";
  const safeCheck = isProviderReplyGrounded(safeReply);
  if (safeCheck === true) {
    safeGroundingResult = "PASS";
  } else {
    console.error('[AOS6 Driver] Safe grounding check failed! Reply was marked ungrounded.');
  }

  // C. Localization matrix (lowercase locale codes: en, tr, de, ru, ar)
  const lowercaseLocales = ['en', 'tr', 'de', 'ru', 'ar'];
  const localizedOutputs = new Set();
  let localizationOk = true;

  for (const loc of lowercaseLocales) {
    const resp = buildGroundedReplacementResponse(loc);
    if (!resp || typeof resp !== 'string' || resp.length === 0) {
      localizationOk = false;
      break;
    }
    localizedOutputs.add(resp);
  }

  // Ensure outputs are non-empty and localized (not all defaulting to single English string)
  if (localizationOk && localizedOutputs.size >= 2) {
    localizationResult = "PASS";
  } else {
    console.error(`[AOS6 Driver] Localization check failed. Output count=${localizedOutputs.size}`);
  }

  // D. No-key provider call (must return success=false, errorCode="AI_PROVIDER_UNAVAILABLE", statusCode=503)
  const noKeyRes = await executeProviderCall({
    aiApiKey: undefined,
    preferredLanguage: "en",
    message: "Synthetic pilot inquiry",
    conversationMessages: [],
    buildSystemPrompt: () => "Synthetic noncanonical system prompt"
  });

  if (noKeyRes.success === false &&
      noKeyRes.errorCode === "AI_PROVIDER_UNAVAILABLE" &&
      noKeyRes.statusCode === 503) {
    noKeyProviderResult = "PASS";
  } else {
    console.error('[AOS6 Driver] No-key provider test failed:', noKeyRes);
  }

  // E. Mock fetch success provider path
  const mockFetchSuccess = async (url, options) => {
    mockProviderCallCount += 1;
    return new Response(JSON.stringify({
      choices: [{ message: { role: 'assistant', content: 'Synthetic assistant grounded response' } }]
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };

  const mockSuccessRes = await executeProviderCall({
    aiApiKey: "synthetic-noncanonical-key",
    aiProvider: "openai",
    aiModel: "synthetic-model",
    preferredLanguage: "en",
    message: "Synthetic pilot inquiry",
    conversationMessages: [],
    buildSystemPrompt: () => "Synthetic noncanonical system prompt",
    fetchImpl: mockFetchSuccess
  });

  if (mockSuccessRes.success === true && mockSuccessRes.rawReply === 'Synthetic assistant grounded response') {
    mockProviderSuccessResult = "PASS";
  } else {
    console.error('[AOS6 Driver] Mock success provider test failed:', mockSuccessRes);
  }

  // F. Mock fetch failure provider path (must catch exception and return statusCode=503, errorCode="AI_PROVIDER_UNAVAILABLE", without leaking sentinel)
  const MOCK_SENTINEL_POISON = "SECRET_MOCK_SENTINEL_EXCEPTION_POISON_12345";
  const mockFetchFailure = async (url, options) => {
    mockProviderCallCount += 1;
    throw new Error(`Synthetic network failure with sentinel ${MOCK_SENTINEL_POISON}`);
  };

  const mockFailRes = await executeProviderCall({
    aiApiKey: "synthetic-noncanonical-key",
    aiProvider: "openai",
    aiModel: "synthetic-model",
    preferredLanguage: "en",
    message: "Synthetic pilot inquiry",
    conversationMessages: [],
    buildSystemPrompt: () => "Synthetic noncanonical system prompt",
    fetchImpl: mockFetchFailure
  });

  const failResStr = JSON.stringify(mockFailRes);
  const sentinelLeaked = failResStr.includes(MOCK_SENTINEL_POISON);

  if (mockFailRes.success === false &&
      mockFailRes.errorCode === "AI_PROVIDER_UNAVAILABLE" &&
      mockFailRes.statusCode === 503 &&
      !sentinelLeaked) {
    mockProviderFailureResult = "PASS";
  } else {
    console.error('[AOS6 Driver] Mock failure provider test failed or sentinel leaked:', mockFailRes, 'sentinelLeaked:', sentinelLeaked);
  }

  const allPass = (
    productStaticQaResult === "PASS" &&
    policyModuleBootResult === "PASS" &&
    unsafeGroundingResult === "PASS" &&
    safeGroundingResult === "PASS" &&
    localizationResult === "PASS" &&
    noKeyProviderResult === "PASS" &&
    mockProviderSuccessResult === "PASS" &&
    mockProviderFailureResult === "PASS" &&
    realNetworkAttempts.count === 0
  );

  const boundedWorkflowResult = allPass ? "PASS" : "FAIL";

  emitResult({
    product_static_qa_attempt_count: productStaticQaAttemptCount,
    product_static_qa_result: productStaticQaResult,
    policy_module_boot_result: policyModuleBootResult,
    unsafe_grounding_result: unsafeGroundingResult,
    safe_grounding_result: safeGroundingResult,
    localization_result: localizationResult,
    no_key_provider_result: noKeyProviderResult,
    mock_provider_success_result: mockProviderSuccessResult,
    mock_provider_failure_result: mockProviderFailureResult,
    mock_provider_call_count: mockProviderCallCount,
    real_provider_network_call_count: realNetworkAttempts.count,
    bounded_workflow_result: boundedWorkflowResult
  });

  if (!allPass) {
    process.exit(1);
  }
}

function emitResult(resObj) {
  console.log(`AOS6_PILOT_DRIVER_RESULT=${JSON.stringify(resObj)}`);
}

runDriver().catch(err => {
  console.error('[AOS6 Driver] Fatal unhandled driver error:', err);
  emitResult({
    product_static_qa_attempt_count: 1,
    product_static_qa_result: "FAIL",
    policy_module_boot_result: "FAIL",
    unsafe_grounding_result: "FAIL",
    safe_grounding_result: "FAIL",
    localization_result: "FAIL",
    no_key_provider_result: "FAIL",
    mock_provider_success_result: "FAIL",
    mock_provider_failure_result: "FAIL",
    mock_provider_call_count: 0,
    real_provider_network_call_count: realNetworkAttempts.count,
    bounded_workflow_result: "FAIL",
    error: "FATAL_DRIVER_EXCEPTION"
  });
  process.exit(1);
});

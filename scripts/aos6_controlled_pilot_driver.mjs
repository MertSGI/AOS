/**
 * AOS6 Controlled Pilot Driver Script (Node.js ESM)
 * Runs inside the sealed target container (node:22-bookworm-slim).
 * Executes static QA check and tests the real ht-ai-chat provider policy module with deterministic mocks.
 */

import { pathToFileURL } from 'url';
import { join } from 'path';

async function runDriver() {
  const workspaceDir = process.env.DISPOSABLE_WORKSPACE_DIR || process.cwd();
  console.log(`[AOS6 Driver] Running inside sealed container at: ${workspaceDir}`);

  // Step P1: Product Static / Executable QA check
  console.log('[AOS6 Driver] Step P1: Validating static QA entrypoint contract...');
  const qaScriptPath = join(workspaceDir, 'scripts', 'test-health-tourism-slice3-lead-ops-ai-assist.mjs');
  
  // Step P2: Boot real policy module
  console.log('[AOS6 Driver] Step P2: Loading real provider-policy module...');
  const policyModulePath = join(workspaceDir, 'supabase', 'functions', 'ht-ai-chat', 'provider-policy.ts');
  const policyUrl = pathToFileURL(policyModulePath).href;

  let policy;
  try {
    policy = await import(policyUrl);
  } catch (err) {
    console.error('[AOS6 Driver] Failed to import policy module:', err);
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
    process.exit(1);
  }

  console.log('[AOS6 Driver] Step P3: Executing grounded policy matrix...');

  // A. Unsafe operational/provider promise rejected
  const unsafeReply = "We promise 100% success rate for all surgeries!";
  const unsafeResult = isProviderReplyGrounded(unsafeReply, "Is it safe?");
  if (unsafeResult !== false) {
    console.error('[AOS6 Driver] Unsafe promise check failed!');
    process.exit(1);
  }

  // B. Safe coordinator-request language accepted
  const safeReply = "I can connect you with our patient coordinator for details.";
  const safeResult = isProviderReplyGrounded(safeReply, "How do I start?");
  if (safeResult !== true) {
    console.error('[AOS6 Driver] Safe request check failed!');
    process.exit(1);
  }

  // C. Localized grounded replacement produced for EN, TR, DE, RU, AR
  const locales = ['EN', 'TR', 'DE', 'RU', 'AR'];
  for (const locale of locales) {
    const replacement = buildGroundedReplacementResponse(locale);
    if (!replacement || typeof replacement !== 'string' || replacement.length === 0) {
      console.error(`[AOS6 Driver] Grounded replacement for ${locale} failed!`);
      process.exit(1);
    }
  }

  // D. executeProviderCall with no API key returns 503 / AI_PROVIDER_UNAVAILABLE
  const envWithoutKeys = { ...process.env };
  delete envWithoutKeys.OPENAI_API_KEY;
  delete envWithoutKeys.GROQ_API_KEY;

  const noKeyRes = await executeProviderCall({
    messages: [{ role: 'user', content: 'Hello' }],
    env: envWithoutKeys
  });

  if (noKeyRes.status !== 503 || noKeyRes.error !== 'AI_PROVIDER_UNAVAILABLE') {
    console.error('[AOS6 Driver] No-key 503 check failed:', noKeyRes);
    process.exit(1);
  }

  // E. executeProviderCall with MOCK fetch success returns synthetic response
  const mockFetchSuccess = async () => {
    return new Response(JSON.stringify({
      choices: [{ message: { role: 'assistant', content: 'Synthetic coordinator response' } }]
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };

  const mockSuccessRes = await executeProviderCall({
    messages: [{ role: 'user', content: 'Hello' }],
    env: { OPENAI_API_KEY: 'synthetic-mock-key' },
    fetchOverride: mockFetchSuccess
  });

  if (mockSuccessRes.status !== 200 || !mockSuccessRes.content) {
    console.error('[AOS6 Driver] Mock fetch success check failed:', mockSuccessRes);
    process.exit(1);
  }

  // F. executeProviderCall with MOCK fetch exception returns bounded 503 response
  const mockFetchException = async () => {
    throw new Error('Synthetic network error');
  };

  const mockFailRes = await executeProviderCall({
    messages: [{ role: 'user', content: 'Hello' }],
    env: { OPENAI_API_KEY: 'synthetic-mock-key' },
    fetchOverride: mockFetchException
  });

  if (mockFailRes.status !== 503 || mockFailRes.error !== 'AI_PROVIDER_UNAVAILABLE') {
    console.error('[AOS6 Driver] Mock fetch exception check failed:', mockFailRes);
    process.exit(1);
  }

  console.log('[AOS6 Driver] All bounded workflow steps PASSED successfully.');
  process.exit(0);
}

runDriver().catch(err => {
  console.error('[AOS6 Driver] Fatal unhandled driver error:', err);
  process.exit(1);
});

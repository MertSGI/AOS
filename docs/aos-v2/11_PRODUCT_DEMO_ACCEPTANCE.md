# Tek gerçek kullanıcı görevi ve bağımsız değerlendirme

**Bu teslimde sonuç: EXECUTABLE_E2E=NOT_RUN; resource gate=WAITING_RESOURCE.** Üstelik V2 API/engine seam'i WP2/WP3'te tamamlanacak. Bu araçlar bugün real success üretmiş değildir.

Test evaluator'ı model/worker uygulayıcısından bağımsız rol üstlenir. Hazırlamada yalnız iki bozuk fonksiyon ve değiştirilemez testler yaratır. Goal API'ye teslim edildikten sonra source/checkpoint/receipt/commit yazamaz. Observer kendi evidence klasörüne gözlem kaydedebilir ve sahipliği kanıtlanmış süreçleri kontrollü durdurabilir. Fixture kodunu elle düzeltmek kabulü iptal eder.

Hazırlık (gerçek mevcut araç):

```powershell
$aosDemoRoot = Join-Path $env:TEMP ('aos-v2-demo-' + [guid]::NewGuid().ToString('N'))
py -3 scripts/aos_v2_evaluator.py prepare --root $aosDemoRoot
```

Çıktı: `workspace/src/discount.py`, `workspace/src/tax.py`, iki functional test, dış sentinel, ilk Git SHA, initial-negative exit1+exit1, hash manifesti ve **enabled=false** grant template. Gerçek resource profili trusted registry sahibi tarafından taze entitlement ile bağlanmadan template yetki değildir. Source düzeltmesi içermez.

Goal: “Repair calculate_discount(price, rate) to return the discounted price and calculate_tax(subtotal, rate) to return the tax amount. Preserve the independent tests. Complete the discount objective and verify/commit it before starting tax. Use only the explicitly authorized free resource, then deliver both verified functions.”

WP2/WP3'ün gerçekleştireceği V2 contract:

```text
POST /v2/goals
X-AOS-Runtime-Token: <read securely inside operator tool>
{"project_id":"aos-v2-disposable","goal":"<goal above>","idempotency_key":"one-real-slice"}
202 {command_id, binding_sha256, state:"QUEUED"}
GET /v2/commands/{command_id} -> snapshot + observed process identity + export status
POST /v2/commands/{command_id}/pause -> boundary-safe; no new side effects
POST /v2/commands/{command_id}/resume -> trusted grant revalidation + same command
```

API reference burada yeni tasarım sözleşmesidir; `/v2` endpoint'lerinin uygulandığı iddia edilmez. Worker'a authority veya grant request body'den geçmez. AOS modelle planı üretir; evaluator hazırlanmış completed-task listesi veya fix vermez.

Gerçek deney sırası:

1. Exact installed source/binary manifest ve V1 protected read-only inventory kaydet. Mevcut 51 commands kökünü cap olmadan `snapshot --root <verified-state-root>/commands --out <evaluator>/v1-before.json` ile, admission registry hash'ini ayrıca kaydet. State root JSON'dan doğrulanır, tahmin edilmez. Eski collector'ı timestamp için tekrar çalıştırmak tek başına hash invariants yerine geçmez.
2. Taze resource eligibility: gerçek native/file capability, credential profile, exact free/included endpoint/model, auth/quota/overage/account evidence; max paid spend0. HTTP 200/free label veya tarihi probe yeterli değil. Bilinmiyorsa RESOURCE_WAIT ve **model çağrısı yok**.
3. Isolated V2 home/loopback API/supervisor başlat; varsayılan PAUSED_SAFE. Sadece yeni disposable project kaydet, AOS'un kaynak/test kapsamını preview et. Normal user entrypoint'ten bu goal bir kez submit et. Engine için `pause_after_verified_task_count=1` evaluator barrier config'i yalnız disposable test profile'ında; bu liste enjekte etmez, gerçek accepted event sonrası yeni dispatch'i durdurur.
4. İlk objective gerçek free-model cevabı, worker PID/nonce, dosya hash'i, independent test exit0, Git commit ve durable accepted task ile gözlenir. Tax hâlâ ilk bozuk hash'inde olmalı. Barrier sonrası evaluator source açısından read-only kalır. Owned engine/supervisor kontrollü stop; gerekiyorsa only verified owned child kill. Sadece server.shutdown ile in-memory completed listesi taşımak process-death proof değildir.
5. Installed source'a dokunmadan aynı home/same command_id ile yeni gerçek engine process'i başlat. Accepted discount task'ın **fiilî** posthash/commit/test'i reconcile edilir, yeniden edit/model/commit yapılmaz. Tax gerçek yeni model çağrısıyla düzelir. Provider değişirse eski agent conversation taşınmaz; source-bound ContextPack kullanılır. Temel kabul için provider değişimi zorunlu değil.
6. Evaluator değişmemiş testleri bağımsız süreçte çalıştırır: `PYTHONDONTWRITEBYTECODE=1 python test_discount.py`, aynı şekilde tax. İki exit0, expected sample values ve test dosya hash'leri aynı olmalı. Git log/diff yalnız izinli src dosyalarını göstermeli; testler/harness sabit kalır. İlk objective tek commit/effect, ikinci objective ayrı doğrulanan etki.
7. Terminal snapshot: PROJECT_COMPLETE, admission CLOSED, tek run.finished, result.json schema/readback, authentic verification receipt ve outbox ack. Outbox pending iken deliverable kabul edilmez. Goal result ile bütün proje ROADMAP kapanışı birbirinden ayrıdır.
8. V1-after uncapped manifests ve admission hash before ile birebir aynı; dış sentineller aynı; izole V2 dışı process öldürülmemiş; LARİ yok; no candidate hot-edit. Sonuncu independent host probe before/after ile byte/provenance sınırını doğrular.

Gözlem aracı (uygulanmış):

```powershell
py -3 scripts/aos_v2_evaluator.py inspect --root $aosDemoRoot --runtime-home <isolated-v2-home> --command-id <observed-id> --out <new-evaluator-observation.json>
py -3 scripts/aos_v2_evaluator.py compare --before <v1-before.json> --after <v1-after.json>
```

`inspect` journal'ı read-only açar, hash/log okur; model, functional test ve billing adına PASS vermez. Beklenen sonuç şeması:

```json
{"schema_version":1,"command_id":"observed-only","source_sha":"exact-40hex","outcome":"REAL_E2E_PASS|RESOURCE_WAIT|FAIL_REPLAN","authority_ref":"trusted-profile-digest","provider_proofs":[],"interruption":{"before_task":"tax","first_task_commit":"observed","old_process_identity":"observed","new_process_identity":"observed"},"first_task_replays":0,"test_exit_codes":[],"terminal_admission":"CLOSED","protected_51_hash_match":true,"billing_proof_ref":"independently-verified","raw_artifact_manifest_sha256":"64hex","limitations":[]}
```

Outcome kararını Controller verir: bütün gerçek chain+account sınırları kanıtlanırsa REAL_E2E_PASS; eligible resource/permission yoksa RESOURCE_WAIT; concrete implementation failure ve tek focused repair sonrası yine kusur varsa FAIL/REPLAN. Production GO bunların hiçbiriyle otomatik verilmez. Task içi fixture receipt alanları veya 10 core regression testi bu JSON'u gerçek kanıtmış gibi dolduramaz.


19Oct Controller delta: binding AOS-V2-SEAM-1 implementation decisions are in14_IMPLEMENTATION_CONTRACT.md; existing five WPs remain. Published source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506 includes bounded portable-scope correction with14local tests; raw first4negative failures preserved. Source CI and operational goal proof are distinct. Windows/sandbox/provider/protected51 live evidence remains a gate; no new semantic-audit coverage percentage or E2E claim is made.

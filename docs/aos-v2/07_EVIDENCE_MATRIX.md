# Kanıt matrisi ve kapsam sınırı

| ID | İddia | Sınıf | Kaynak / karşı kanıt |
|---|---|---|---|
| E01 | Main exact SHA 9d3168afb35edf5ad06e6b5e419801493fe04572 | REMOTE_VERIFIED | GitHub branch GET + authenticated git clone/ls-remote |
| E02 | Main CI39105714749 SUCCESS | REMOTE_VERIFIED | run.head_sha exact main; user goal E2E değil |
| E03 | B11 e038072… CI39110437356 SUCCESS, ayrı dal | REMOTE_VERIFIED | diff5 files + authority/router/backend kaynak; deployment acceptance yok |
| E04 | ZIP19/19 internal hashes MATCH | HOST_OBSERVED archive bytes | support_archive_receipt.json; host runtime değil |
| E05 | 51 kayıt, 0ACTIVE, 9RUNNING; manifest242; KCP648 | HISTORICAL_REPORTED | kullanıcı JSON19:02:52Z; schema semantics/process/billing kanıtı değil |
| E06 | Terminal state yazımı admission ACTIVE bırakır | ISOLATED_REAL storage experiment | discriminating_probe.json; actual model yok |
| E07 | Event append crash sonrası seq[1,2,2] | ISOLATED_REAL subprocess/disk | child exit73; real user task recovery testi değil |
| E08 | Goal completion canonical status substring'e bağlı | SOURCE_INSPECTED | planning_kernel.detect_completion; canlı false-PASS etkisi UNKNOWN |
| E09 | Direct code-generation call common quota gate'i atlar | SOURCE_INSPECTED | model_native_backend._call_model; free label account proof değil |
| E10 | New journal+scope+intent+outbox test10PASS | ISOLATED_REAL storage + SIMULATED verifier receipts | tests/test_aos_v2_lifecycle.py; actual provider/engine yok |
| E11 | Evaluator fixture her iki başlangıç testinde exit1 | ISOLATED_REAL preparation | evaluator self-check; kod düzeltilmedi, E2E NOT_RUN |
| E12 | Windows source/process/account canlı state | UNKNOWN | connector filesystem access yok; yeni local probe gerekir |
| E13 | Common controller Google Doc okundu | REMOTE_VERIFIED journal history | document ID1IH9AI…; AOS/LARI ayrı; tarihli raporlar canlı değil |

Tam structural inventory:504/504 tracked dosya hash/byte envanteri (%100),296/296 Python AST parse (%100),1176 static import edge,12 dynamic import site. Runtime module kapsamı src/extensions test hariç151 modül,64807 satır.
**Targeted semantic sampling:25/151 modülde en az bir gövde/yol (%16.56). Bu tüm satırların semantik denetimi değildir.** Kritik A–E zincirleri 02'de tek tek işlendi; kalan modüllerin ayrıntısı UNKNOWN olarak envanterde korunur. Yüzdeyi büyütmek için docstring okumak semantik doğrulama sayılmadı. Full code correctness/Windows runtime coverage %100 iddiası yoktur.

Kaynak erişimi ve mevcut Linux dosya/süreç deneyleri bağımsızdır; kullanıcı Windows JSON'u kopya kanıtıdır. Her 'works real' iddiası kendi trust boundary'sinde ayrıca ispat ister. Artifact sayıları ürün başarı metriği değildir.

## Yerel geniş regresyon kaydı

İlk koşu:12 failed,1619 passed,11 skipped,26 deselected /600.15s. İzole alt interpreter bağımlılıkları, SOCKS extra, format validator ve eksik Chromium saptandı. Test ortamı düzeltilince başarısız6 browser-dışı node yeniden koşuldu:6 passed /24.90s.6 browser node Chromium indirmesi engellendiği için yerelde BLOCKED_EXTERNAL. Tüm suite temiz PASS denmiyor. İlk hata kaydı ve hash/sanitize manifesti evidence dizininde korunur. Yeni journal10testi ilk koşuda geçmiştir. Hosted exact-SHA CI sonucu dış teslim receipt'i ve GitHub'dan okunur; kaynak dosyalarına gelecek run ID uydurulmaz.


19Oct Controller delta: binding AOS-V2-SEAM-1 implementation decisions are in14_IMPLEMENTATION_CONTRACT.md; existing five WPs remain. Published source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506 includes bounded portable-scope correction with14local tests; raw first4negative failures preserved. Source CI and operational goal proof are distinct. Windows/sandbox/provider/protected51 live evidence remains a gate; no new semantic-audit coverage percentage or E2E claim is made.

# AOS V2 — Önce bunu okuyun

Karar: **HYBRID_V2**. Kaynak tabanı `9d3168afb35edf5ad06e6b5e419801493fe04572`. Dal `feature/aos-v2-thin-lifecycle-20261018`.
Bu çalışma korunan Windows Runtime V1'e bağlanmaz, onu değiştirmez; eski 51 komutu içeri almaz.

**Tek canonical program kaydı:** `docs/project-control/STATE.json` içindeki `extensions.aos_v2`.
Kararlar mevcut `docs/project-control/DECISIONS.md`, kanıtlar mevcut `EVIDENCE.jsonl` dosyasına eklenir.
Bu dizin bunların tasarım/uygulama referansıdır; ikinci bir STATE veya ikinci karar ledger'ı yaratılmadı.
Çalışan görevlerin SQLite journal'ı program yönetişiminin yerine geçmez.

Başlangıç: [RESUME_HERE.md](RESUME_HERE.md). Ayrıntı sırası: 12 → 02 → 04 → 05 → ilgili executor paketi.
`06_EXECUTOR_PACKETS/02_EXECUTION_SEAM.md` sonraki tek kaynak işidir. WP1'i tekrar başlatmayın.
Doküman dizisinin sırası çalışma aşaması veya tamamlanma kanıtı değildir.

Kanıt sözlüğü:

| Sınıf | Anlam |
|---|---|
| REMOTE_VERIFIED | GitHub/Drive üzerinden bu oturumda okunan sürüm |
| SOURCE_INSPECTED | Exact kaynakta izlenen mekanizma; canlı çalıştı iddiası değil |
| ISOLATED_REAL | Geçici Linux dosyası/süreci üzerinde gerçek deney; belirtilen sınırla |
| SIMULATED | Model/worker/evidence yerine fixture kullanılmış kontrat testi |
| HISTORICAL_REPORTED | Kullanıcı JSON'u veya yürütücü raporundaki tarihli gözlem |
| HOST_OBSERVED | Yalnız doğrudan erişilebilen mevcut host; Windows için bu sınıf yok |
| INFERRED | Kaynak ve deneyden mimari çıkarım |
| UNKNOWN | Erişilmemiş/doğrulanmamış sınır |

**Teslim durumu:** kaynak journal temeli ve değerlendirici araçları uygulanmıştır; V2 engine/API ve gerçek provider bağlantısı henüz uygulanmamıştır. `REAL_E2E_PASS` yoktur. Windows süreçleri, hesap kotası ve billing bu oturumda gözlenmemiştir.


19Oct CURRENT: source published at `7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506` on `feature/aos-v2-thin-lifecycle-20261018`; old HTTPS/integration blocker superseded by authenticated owner web publication. Public approval persists, no owner ZIP upload needed.14journal tests pass; exact-source/full carrier CI must be independently read. Engine/API/gateway still not implemented; realE2E NOT_RUN. Binding implementation contract:14_IMPLEMENTATION_CONTRACT.md. Same five WPs; source work follows existing02packet after WP1 acceptance. Historical publication failures remain in EVIDENCE.jsonl and their receipt files.

# KEEP / MODIFY / REMOVE / REPLACE / ADD

Bu tablo Controller tasarım kararıdır (INFERRED); satırlardaki kanıt ayrıntıları 02 ve 07'dedir. Kaynakta mevcut olmak canlı başarı anlamına gelmez.

| Modül / sınır | Karar | Gerekçe / sahibi / kabul |
|---|---|---|
| Protected Runtime V1 ve 51 geçmiş komut | KEEP, read-only coexistence | Kullanıcı koruması; geçmiş byte'lar taşınmaz/replay edilmez; operator owner |
| `runtime_supervisor`, `runtime_slots`, `provenance`, `runtime_assets` | KEEP | Identity/immutable deployment değerli; V2 için ayrı home/port; WP5 source+host QA |
| `runtime_store` + `runtime_admission` yeni görev sahipliği | REPLACE seam | SQLite transaction journal; V1 dosyaları kendiliğinden migrate olmaz; WP1 |
| `planning_kernel.run_autonomous_project` ve `detect_completion` V2 ana loop'u | REPLACE seam | 5k satırlık project/governance replanning yerine immutable goal DAG ve bağımsız goal predicates; WP2 |
| `native_workers.NativeFileWorker`, exact/patch engine | KEEP with binding | SHA/UTF8/newline/overlap kontrolleri korunur; grants + intent + posthash; WP2 |
| `ModelNativeImplementationBackend` transport/commit/rollback | MODIFY/extract | Shared provider gateway; private stage, no hard reset, git commit intent/trailer; WP2 |
| `provider_registry`, circuits, observation, quota, adequacy | KEEP / consolidate gateway | Her gerçek çağrı aynı non-paid gate; planner ile editor ayrı kaçış yok; WP2 |
| `ResourceOrchestrator` eligibility/scarcity | KEEP pure ranking | Score izin değildir; trusted profile, cost/entitlement deny önce; WP2 |
| `PersistentCoordinator` / `run_registry` / integrity claims | KEEP V1; one V2 owner | Aynı V2 task'ı eski coordinator ve yeni engine birlikte yönetmez; fingerprint fikirleri taşınır |
| KCP append-only receipt ve source provenance | KEEP | Karar/evidence ledger'ı task scheduler değil; idempotent outbox exporter; WP2/WP3 |
| KCP indexes / Drive mirror / second brain | OPTIONAL derived | Görev kapanışını yorumdan çıkarmak yasak; export hatası görünür teslim blocker'ı |
| `control_panel` + `local_host` legacy queue fallback | REMOVE from V2 path | İki entrypoint/queue arasında sessiz fallback yok; V2 API uyumlu tek küçük view; WP3 |
| `completion_supervisor` + generic evidence PASS | REMOVE from V2 verdict path | Backend SUCCESS/dag-empty görevi kapatmaz; goal verifier tek acceptance owner |
| Coordinator empty FILE/PROCESS payload test defaults | REMOVE from V2 production path | `.aos_done` veya python pass ile başarı üretmek gerçek iş değildir; boş plan/payload hard deny |
| Full graph council / specialist fabric / JEV / design critics | OPTIONAL disabled | Code-repair vertical slice için zorunlu değil; değerleri ancak ayrı iş tipinde ölçülür |
| Multi-PC Postgres / supervisor pools | OPTIONAL preserve | İlk ürün tek makine; yeni always-on DB yok; eski kanıtlı capability silinmez |
| Self-repair, source-repair factory, automatic remote publishing | OPTIONAL disabled | Hatalı kontrol katmanı kendi yetkisini genişletemez; kullanıcı kritik kapısı |
| AG/Codex/Cline + structured planning bridges | OPTIONAL default deny | Scarce resources, trusted authority/entitlement/binary proof; LARİ session reuse yok |
| B11 `antigravity_authority` + adapter delta | HOLD; no cherry-pick | Missing authority bypass ve request self-grant bağımsız source bulgusu |
| Local Qwen / FreeLLMAPI | OPTIONAL no automatic start | Current headroom/auth/runtime proof yok; base acceptance öncesi etkinleşmez |
| `src/aos/v2/lifecycle.py` | ADD, source implemented | Transactional lifecycle + immutable task intent + outbox; internal primitive, engine değil |
| `scripts/aos_v2_evaluator.py` | ADD, preparation tools implemented | Faulty fixtures, complete hash snapshots, read-only structural inspector; kendi E2E PASS'ini üretmez |

**Gerçek silinen dosya: 0.** Bu aşamada 'REMOVE' V2 yürütme yolundan çıkarma kararıdır. Tarihsel kayıtlar, eski public API ve dinamik CLI consumer'ları doğrulanmadan fiziksel silme yapılmaz. `NO_STATIC_CONSUMER` sadece adaydır, DEAD kanıtı değildir. Tüm dosyalar için owner/risk/consumer envanteri `evidence/module_inventory.csv` dosyasındadır.

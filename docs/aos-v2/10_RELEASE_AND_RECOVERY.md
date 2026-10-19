# Yan yana işletim, göç, yayın ve geri dönüş

1. V1 home/config/slots/knowledge/51 commands/legacy worktrees salt okunur korunur. Yeni V2 home `%LOCALAPPDATA%/AOS/runtime-v2-disposable-<unique>`; farklı loopback port (örn. boş olduğu doğrulanmış 8871), farklı token/nonce. Token argv veya log'a yazılmaz. Otomatik default Product profile yok.
2. Yeni V2 journal schema v1; V1 dosyalarını import eden otomatik migration yok. Salt okunur legacy görünüm `LEGACY_HELD_HISTORY`, eski state/admission ayrı alanlar, yürütülemez. Gerekirse yalnız kopya snapshot üstünde idempotent migration mapping: old cid→legacy source ref+hash; new goal için yeni explicit grant/idempotency key; görev tamamlanmış kabulü için gerçek Git+postcondition kontrolü. Eski RUNNING/ACTIVE label'ları yeni yetki olmaz.
3. WP4 doğrulanana kadar korunan runtime deploy edilmez. Yeni candidate source exact SHA, clean tree, compiled/source manifest, platform bağımlılıkları ve immutable install yolu ile stage edilir. V1 materializer `aos/v2` paketini ve entrypoint'ini içeriyor mu explicit inventory/import test gerekir; içerdiği varsayılmaz.
4. Yeni V2 state backup'ı: scheduling'i PAUSED_SAFE yap; owned worker'ların boundary'de durduğunu doğrula. SQLite backup API'si ile tutarlı snapshot al (WAL açıkken yalnız journal.sqlite3 kopyalama). Resource/KCP outbox/export receipts, grants'in nonsecret sürümü, source/candidate hash ve yeniden kurma komutları manifestte. Credentials secure store'da ayrı; secrets backup'a açık metin konmaz.
5. Restore yalnız yepyeni disposable home'a. Token/nonce yenile, startup disabled, PAUSED_SAFE; journal integrity_check + source identity + task receipts/artifacts/Git refs + outbox state doğrula. Pending external effect otomatik replay edilmez. Accepted task hashes uyuşmazsa HUMAN_REQUIRED. Geçmiş 51 dışarıda kalır.
6. Owner onayları ayrı somut sınırları adlandırır: Windows isolated grant/provider hesabı bağlama gerekiyorsa tek owner action; protected stage/maintenance activation; stable promotion; production/real Product kullanımı; startup. Kaynak candidate'ı incelemeye sunmak bu onayların yerine geçmez.
7. Live prosedür mevcut native akışı korur: fresh read-only identity/hash/process/command snapshot → quiesce → native stage/validate → ayrı authorized maintenance activate `--without-startup` → observed API/panel/nonce/slot health → ayrı stable promotion → gerekirse yeni grant'lı görevler. Her adım execution receipt'ine bound, eski komut topluca resume yok. Pointer flip sıfır kesinti garantisi değildir.
8. Rollback: yeni V2 scheduling stop + bounded owned children shutdown; V1 dosyalarına dokunulmadıysa eski stable API/nonce/health'in hâlâ doğru olduğunu observe et. Native restore gerekiyorsa exact mevcut transaction/candidate kanıtını kullan, eski transaction ID uydurma. Pointer metadata PASS sağlık PASS değildir. Runtime geri döner; yeni V2 journal ve Git değişiklikleri forensic olarak korunur, silinmez.

Yan etki pencereleri:

| Ölüm noktası | Recovery |
|---|---|
| Task intent öncesi | Side effect olmamalı; başlatılabilir |
| Intent sonrası, effect sonucu belirsiz | RECONCILE_REQUIRED; posthash/commit/test kanıtı görülmeden rerun yok |
| Dosya yazılmış, Git commit yok | Private stage ve immutable original/posthash üzerinden güvenli doğrulama; kullanıcı değişikliği varsa HOLD |
| Git commit olmuş, task receipt yok | Expected parent/tree/path/trailer + bağımsız test doğrulanır; aynı commit kabul edilir, ikinci commit yok |
| Journal finalize COMMIT öncesi | Eski state/admission/result/event tutarlı; transaction rollback |
| Journal COMMIT sonrası, JSON/KCP export öncesi | CLOSED admission; outbox yeniden export edilir, worker çalıştırılmaz |
| Export sonrası ack öncesi | Idempotency key aynı; artifact/readback verify, yalnız ack |

Bu tablo tasarımdır. WP1 process-death transaction satırlarını test eder; Git/effect/export recovery kanıtı WP2–WP4'te henüz gereklidir. Power outage, disk loss, Windows ownership ve offsite restore UNKNOWN'dır.

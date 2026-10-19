# Ürün sözleşmesi

**INFERRED / Controller kararı:** AOS; açıkça yetkilendirilmiş bir yazılım hedefini kendi planlayıp uygun ücretsiz/aboneliğe dahil kaynaklarla çalıştıran, sonucu doğrulayan ve kesintiden kaldığı işten güvenle dönen Windows öncelikli yerel araçtır. AG bir agentic işçidir; sıradan chat provider değildir. AOS planın sahibi, worker sınırlandırılmış uygulayıcıdır.

| Kullanıcı adımı | Sistem sözleşmesi | Kabul kanıtı |
|---|---|---|
| Projeyi seç | Boş/default proje reddi; kayıtlı kök ve grant özeti | Geçersiz istek worker/admission yaratmaz |
| Hedefi yaz | Bir idempotency key → bir command_id; goal kriterleri sabitlenir | Aynı istek aynı ID; değiştirilmiş gövde conflict |
| Yetkiyi gör | Trusted registry'deki kaynak SHA, dosyalar, komutlar, no-paid sınırı | Payload yetki genişletemez; süresi/geçerliliği her etkiden önce kontrol edilir |
| Başlat | AOS değişiklik planını üretir ve DAG'ı deterministik doğrular | Plan hash'i, gerçek model request/response referansı, task listesi |
| İlerlemeyi izle | Kaynak seçimi ve eleme nedeni, doğrulanan iş, beklenen kaynak | Backend yok / auth yok / kota / güvenlik ayrıdır |
| Kesintiden dön | Aynı komut; accepted artifact tekrar kontrol; belirsiz yan etki reconcile | İlk işin commit/hash'i değişmez, ikinci iş sürer |
| Teslim al | Bağımsız test, Git SHA/diff, result/receipt, CLOSED admission | Export pending iken UI 'teslim edildi' demez |

İlk MVP: tek makine, tek mutation worker, tek disposable proje, sınırlı kaynak dosyası ve önceden yetkili test argv'leri. Native yeterliyse model çağırma. Olağan plan/kod üretiminde uygun gerçek ücretsiz provider kullan; task gerektirmedikçe AG/Codex kotası harcama. Bilinmeyen endpoint/cost/entitlement için WAITING_AUTHORITY veya WAITING_RESOURCE; ücretliye geçiş yok.

16GB/i5/GPU yok: aynı anda tek mutation worker, sınırlı output/context; Qwen otomatik başlatılmaz. Qwen mevcut headroom ve model+binary attestation sonrası, ölçülmüş güvenli eşik (tarihsel 4.5GB asgari tek başına kapasite garantisi değil), idle teardown ile ayrı opsiyonel paket olur. FreeLLMAPI olmadan direct free yol çalışabilmelidir. `agy` kurulu varsayılmaz.

MVP dışı: multi-PC scheduler, deliberation council, model uzmanlar federasyonu, UI tasarım critic ensemble, otomatik self-repair/source publish, provider keşif daemon'u, sürekli Qwen, zorunlu FreeLLMAPI, AG re-entry demosu, Product/LARİ pilotu, startup ve production. Bunlar silinmiş veya değersiz sayılmaz; temel görev kabulüne bağımlıdır.

Güvenlik sınırı: bir argv allowlist veya sentinel OS sandbox değildir. İlk dilim yalnız trusted disposable test kodu çalıştırır. Düşmanca repo/agent code'u için restricted OS identity/sandbox ile dosya ve ağ yetkisi ayrıca kanıtlanmadan genel ürün kullanımına açılmaz. Test ortamına provider secret verilmez. Modelden gelen komutlar çalıştırılmaz.

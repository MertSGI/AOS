# Azaltım ve durma kuralları

**Ölçülen baseline:** `planning_kernel`5284 + `runtime_worker`1626 + `runtime_server`2127 + `control_panel`4782 + `persistent_coordinator`634 satır farklı düzeylerde plan/state/status sahibidir. Bunların tamamı çöpe atılmayacak. İlk V2 path bu loop'ları aynı görev için üst üste çalıştırmayacak. Lifecycle core yeni ve stdlib SQLite tabanlıdır. Yeni kaynak satırları artifact manifestinde sayılır.

| Azaltım | Somut kazanç | Sayısal sınır |
|---|---|---|
| Yeni command için state/result/admission/event-seq dosya writer'ları yerine bir DB tx | Ghost ACTIVE ve duplicate seq penceresini kapatır | V1 crash repro vs V2 actual tx crash tests; uptime yüzdesi ölçülmedi |
| Aynı task'a planner/coordinator/supervisor completion kararlarını üst üste bağlamama | Bir goal acceptance sahibi | Fewer ownership edges; başarım/RAM tasarrufu benchmark edilmedi |
| Direct NVIDIA mutation transport'u ortak gateway'e alma | Tek accounting/deny/retry yeri | Model çağrısı sayısı outcome bazında ölçülecek; mevcut bypass kaynakta kanıtlı |
| Self-repair/council/design/mirror/multi-PC'yi başlangıç path'inden çıkarma | Code repair için gereksiz wake/import/LLM çağrısı yok | Kaynak call graph tasarımı; canlı latency tasarrufu iddia edilmiyor |
| KCP append'i receipt exporter'a alma, task scheduling journal'ını ayırma | Append-only tarih korunur, yeniden tarama hot loop'tan çıkar | KCP full hash scan source mevcut; O(N) append davranışı, gerçek büyüme benchmark'ı yok |
| Legacy default Product ve UI queue fallback çıkarma | Yanlış projeye istemsiz task engeli | Negative API tests WP2/3 kabulüne bağlı |

Fiziksel deletion şu anda0; startup/servis azaltımı0 (live'a dokunulmadı). Satır/dosya sayısı hedef değil, bağımsız user-goal completion ana ölçüdür. ROI yüzdesi, haftalık kota tasarrufu veya 16GB'da FPS gibi dayanaksız sayı yok.

STOP: unknown-cost/paid route; missing/forged authority; scope escape; secret-bearing raw logs; unexpected source/host SHA; V1/51 veya LARİ değişikliği; unrelated staged files/unknown commit; worker ownership ambiguity; insufficient headroom; actual test/hash evidence yerine mock receipt; bir root-cause repair sonrası aynı replay/atomicity hatası. Scope veya mimari genişlemesi gerekiyorsa canonical issue/ADR güncelle, aynı işi yeni faz adıyla tekrar çalıştırma. Resource absence yalnız typed wait, yeni ücretli satın alma veya architecture churn nedeni değil.


19Oct Controller delta: binding AOS-V2-SEAM-1 implementation decisions are in14_IMPLEMENTATION_CONTRACT.md; existing five WPs remain. Published source7afa0e1d4c7768b6a905a2d2987ef2e54b8f0506 includes bounded portable-scope correction with14local tests; raw first4negative failures preserved. Source CI and operational goal proof are distinct. Windows/sandbox/provider/protected51 live evidence remains a gate; no new semantic-audit coverage percentage or E2E claim is made.

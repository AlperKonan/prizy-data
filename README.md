# prizy-data

Türkiye'deki halka açık elektrikli araç şarj istasyonları — Prizy uygulamasının veri kaynağı.

- **Kaynak:** EPDK Serbest Erişim Platformu, `https://apigateway.epdk.gov.tr/sarjIstasyonlari` (resmi, kimlik doğrulamasız REST servisi).
- **Güncelleme:** her gece 04:17 (TR), GitHub Actions ile tek istek.
- **Yayın:** GitHub Pages → `https://alperkonan.github.io/prizy-data/<dosya>.json`

| Dosya | İçerik |
|---|---|
| `meta.json` | Üretim zamanı ve her dosyanın sürüm hash'i (uygulama önce bunu okur) |
| `stations.json` | Halka açık istasyonlar: konum, operatör, soketler (tip, standart, kW) |
| `brands.json` | Operatör/marka sözlüğü (elle bakım) |
| `prices.json` | Operatör tarifeleri, kaynak ve kontrol tarihiyle (elle bakım) |
| `vehicles.json` | Araç marka listesi |

Canlı doluluk, arıza ve fiyat EPDK servisinde **yoktur**; fiyatlar operatörlerin resmi sitelerinden elle girilir ve bilgi amaçlıdır.
Gerçek kişi adı içerebilecek `sarjIstasyonuIsletmecisi` alanı yayınlanmaz.

## Yerel çalıştırma
```bash
python3 scripts/test_build_stations.py                                   # testler (istek atmaz)
python3 scripts/build_stations.py --from-raw fixtures/epdk_raw.json      # kayıtlı yanıttan üret
python3 scripts/build_stations.py --meta-only                            # elle düzenlemeden sonra sürümleri güncelle
python3 scripts/build_stations.py --save-raw fixtures/epdk_raw.json      # gerçek istek (saatte 1 kota!)
```

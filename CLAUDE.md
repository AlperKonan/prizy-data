# prizy-data — Proje Kuralları (Claude Code bu dosyayı her oturumda okur)

Bu repo Prizy mobil uygulamasının **public veri hattıdır**. Uygulama kodu ayrı, private repoda (`prizy-v2`).
Uygulama EPDK'ya asla doğrudan gitmez; bu reponun GitHub Pages'te yayınladığı `public/*.json` dosyalarını okur.

## Yapı
```
scripts/build_stations.py        veri hattı (sadece Python standart kütüphanesi)
scripts/test_build_stations.py   birim testleri — gerçek istek ATMAZ
public/                          yayınlanan dosyalar (GitHub Pages kökü)
fixtures/                        ham EPDK yanıtı (gitignore'da, repoya girmez)
.github/workflows/refresh.yml    günlük çekim + Pages yayını
```
Pages adresi: `https://alperkonan.github.io/prizy-data/<dosya>.json`

## EPDK servisi — doğrulanmış gerçekler (tahmin etme, bunlara uy)
- Swagger: https://apigateway.epdk.gov.tr/sarjIstasyonlari?swagger — auth YOK.
- İstek: `GET https://apigateway.epdk.gov.tr/sarjIstasyonlari/` + header `Content-Type: application/json` + **body** `{}` (GET ama gövdeli; tarayıcı fetch'i bunu yapamaz, Python urllib/curl yapar).
- Filtreli sorgu alanları: lisansNo, sarjIstasyonuNo, markaAdi, yesilSarjIstasyonuMu, sarjIstasyonuAdi, hizmetSekli.
- KOTA: filtresiz saatte 1, filtreli dakikada 1. Aşılınca HTTP 429 `{"fault":{"faultString":"...QUOTA..."}}`. Yeniden deneme YAPMA.
- Aşağıdaki şema 30.09.2026 tarihli gerçek çekimle doğrulandı (16.888 kayıt, 13.076 halka açık, 48.871 soket).
- Yanıt üst düzeyi: `statusCode` (200), `statusDescription` ("OK"), `message`, `columnNames` (alan adları listesi), `numRows`, `result`, `elapsedTime`, `errors` ([]), `data` ([...]).
- Kayıt alanları (15, hepsi her kayıtta var): sarjIstasyonuNo ("ŞRJ/1000", tekil), sarjIstasyonuAdi, yesilSarjIstasyonuMu, hizmetSekli, sarjAgiIsletmecisiUnvan, sarjAgiIsletmecisiLisansNo, sarjIstasyonuIsletmecisi, marka, olumluGorusVerenDagitimSirketiLisansNo, olumluGorusVerenDagitimSirketiLisansUnvani, dagitimSirketiOlumluGorusBelgeNumarasi, soketler[], adres, enlem, boylam.
- Tipler: enlem/boylam float; diğer her şey string. hizmetSekli yalnızca "HALKA_ACIK" / "OZEL". yesilSarjIstasyonuMu string: "EVET" / "HAYIR" (boolean değil).
- Soket alanları (hepsi string): soketNo ("SKT/18029", tekil — yayınlanmıyor, bkz. PLAN.md), soketTipi ("AC"/"DC"), soketTuru (yalnızca "AC_TYPE2", "DC_CCS", "DC_CHADEMO"), soketGucu ("22", "180" gibi; bu çekimde boş yok ama boş olabilir diye ele al).
- Bilinen veri kusurları: soketler listesi boş olabilir (3 istasyon); soketTipi ile soketTuru çelişebilir (1 soket) → soketTuru önekine güven.
- Canlı durum, fiyat, arıza alanı YOK. Uydurma.
- Eski `lisansws.epdk.gov.tr/...SorgulaPublic` uçları KAPALI (404). Kullanma.
- Geliştirmede EPDK'ya tekrar tekrar istek atma: `fixtures/epdk_raw.json` üzerinden `--from-raw` ile çalış.

## Veri formatları (yayınlanan dosyalar)
Yayınlanan dosyaların hepsi `public/` altındadır: `meta.json`, `stations.json`, `prices.json`, `brands.json`, `vehicles.json`.

### public/meta.json  (uygulama önce bunu çeker, küçük)
```json
{
  "generatedAt": "2026-10-01T01:17:00Z",
  "stationsVersion": "sha256-ilk12",
  "pricesVersion": "sha256-ilk12",
  "brandsVersion": "sha256-ilk12",
  "vehiclesVersion": "sha256-ilk12",
  "pricesUpdatedAt": "2026-09-30",
  "stationCount": 13074
}
```
`*Version` alanları ilgili dosyanın içerik hash'idir (sha256'nın ilk 12 karakteri). Uygulama her dosyayı — stations, prices, brands, vehicles — **kendi sürümü değişince** indirir.
`stationsVersion` yalnızca istasyon listesinden hesaplanır, `generatedAt` hash'e girmez; veri aynıysa sürüm de aynı kalır ve uygulama yeniden indirmez.

### public/stations.json
```json
{
  "generatedAt": "...",
  "source": "EPDK Serbest Erişim Platformu (apigateway.epdk.gov.tr/sarjIstasyonlari)",
  "stations": [
    {
      "id": "ŞRJ/1000",
      "name": "Kerimaba Petrol Arnavutköy",
      "brand": "zes",                 // brands.json'daki slug
      "operator": "ZES DİJİTAL TİCARET A.Ş.",   // sarjAgiIsletmecisiUnvan (tüzel kişi)
      "address": "…",
      "lat": 41.17774, "lng": 28.748019,
      "green": false,
      "sockets": [ { "type": "DC", "standard": "CCS", "kw": 60 } ],
      "acCount": 0, "acMaxKw": null, "dcCount": 2, "dcMaxKw": 60
    }
  ]
}
```
Kurallar: sadece `hizmetSekli` = halka açık; koordinatı Türkiye kutusu dışında (lat 35–43, lng 25–45) olanlar atılır; soketi olmayan istasyon atılır (raporda sayılır); `sarjIstasyonuIsletmecisi` yayınlanmaz; id'ye göre sıralı, her istasyon tek satırda (günlük diff okunur olsun).
`green`: EPDK `yesilSarjIstasyonuMu` "EVET" → true, "HAYIR" → false.
`soketNo` ("SKT/18029", tekil) V1'de **yayınlanmaz**. Not: ileride canlı doluluk için operatör verisini sokete bağlamanın anahtarı bu alandır; gerektiğinde `build_stations.py` → `build_sockets` içindeki yorum satırı açılarak tek satırla yayınlanır.
İlk gerçek çekim (30.09.2026): 16.888 kayıt → 13.073 istasyon, 36.203 soket; `stations.json` 5,8 MB (gzip 0,8 MB).

### public/brands.json (elle bakım, bir kez üretilir sonra düzenlenir)
```json
[{ "slug": "zes", "name": "ZES", "aliases": ["zes", "ZES"], "appStore": "…", "playStore": "…", "color": "#…" }]
```
EPDK'daki ham `marka` yazımları farklı (zes / eşarj / VOLTRUN). Eşleşme slug üzerinden yapılır (büyük/küçük harf, Türkçe karakter ve boşluk farkı önemsiz). Eşleşmeyen marka → otomatik slug + raporda uyarı.
Opsiyonel alanlar: `licenseNos` (`sarjAgiIsletmecisiLisansNo` listesi) ve `note`. Aynı yazımı farklı operatörler kullanıyorsa eşleşme marka + lisans no ile yapılır (ör. `greenwatt` ve `greenwatt-gwesi`). Aynı slug'a düşen farklı operatörler her çalıştırmada raporlanır.
Taslak `build_stations.py --from-raw … --draft-brands` ile üretilir; elle yapılan düzenlemeleri ezmez. `name`, `appStore`, `playStore`, `color` P4'te doldurulur.
Uygulama marka adı, renk ve mağaza linkleri için bu dosyayı indirir; bu yüzden `public/` altında yayınlanır.

### public/vehicles.json (elle bakım)
```json
[{ "slug": "togg", "name": "Togg" }]
```
Araçlarım ekranındaki marka listesi. Eski API'deki `vehicle.json`'dan taşındı (383 marka, sadece `slug` + `name`; kaynakta model bilgisi yok, model ve plakayı kullanıcı yazar).
P7 notu: elektrikli araç satmayan markalar ayıklanacak; Türkiye'de EV satan markalar listenin başına alınacak.

### Araç logoları — KARAR: logo yok, marka baş harfleri
Araç markası logo yerine baş harfleriyle gösterilir. Logo dosyası kopyalanmaz, `public/vehicle-logos/` oluşturulmaz, `vehicles.json`'da logo alanı bulunmaz.
Gerekçe (30.09.2026 kontrolü): `filippofilip95/car-logos-dataset` README ve `package.json`'da "MIT" diyor ama repoda LICENSE dosyası yok; README logo görsellerinin sahiplerine ait olduğunu ve kendi lisans koşullarına tabi olduğunu yazıyor. MIT kodu/metadatayı kapsar, logoları değil.

### public/prices.json (elle, GitHub web arayüzünden düzenlenir)
```json
{
  "updatedAt": "2026-09-30",
  "brands": {
    "zes": {
      "tariffs": [
        { "type": "AC", "label": "AC", "pricePerKwh": null },
        { "type": "DC", "label": "DC ≤ 100 kW", "maxKw": 100, "pricePerKwh": null },
        { "type": "DC", "label": "DC > 100 kW", "minKw": 100, "pricePerKwh": null }
      ],
      "sourceUrl": "https://…", "checkedAt": "2026-09-30", "note": ""
    }
  }
}
```
Fiyatı doğrulanmamış marka → `pricePerKwh: null` → uygulamada "Fiyat bilgisi yok". Uygulama her fiyatın altında "Kaynak: operatör sitesi · kontrol: 30.09.2026" gösterir.

## Veri kuralları (veri hattı ve uygulama aynı kuralı uygular)
- **Hız sınıfı:** `kw ≤ 22` normal; `22 < kw ≤ 100` hızlı; `kw > 100` ultra.
- **Tarife kademesi eşleşmesi:** `(minKw yoksa veya kw > minKw) VE (maxKw yoksa veya kw ≤ maxKw)`. Alt sınır hariç, üst sınır dahil; tam 100 kW "≤ 100" kademesine düşer.
- **Gücü boş soket:** `kw = null`; hız sınıfı "bilinmiyor"; hız filtrelerinde sadece "Tümü"de görünür; `acMaxKw` / `dcMaxKw` hesaplarına katılmaz; ekranda "— kW".
- **id:** `sarjIstasyonuNo` olduğu gibi kullanılır. Veri hattı tekrarlanan id'leri raporlar ve tekilleştirir (ilk kayıt kalır, uyarı basılır). İlk gerçek çekimde tekrar yok (0).
- **Soket tipi:** `soketTipi` ile `soketTuru` çelişirse `soketTuru` önekine (`AC_` / `DC_`) güvenilir; raporda sayılır. `standard` eşlemesi: `AC_TYPE2` → "Type 2", `DC_CCS` → "CCS", `DC_CHADEMO` → "CHAdeMO".
- **hizmetSekli normalizasyonu:** değer casefold edilir ve "halka" önekiyle başlıyorsa halka açık sayılır ("HALKA_ACIK", "Halka Açık" vb.). Gerçek değerler: yalnızca `HALKA_ACIK` ve `OZEL`.

## Veri hattı davranışı
- Tek istek, 180 sn timeout, yeniden deneme yok.
- 429 / 5xx / ağ hatası → yayınlama, önceki dosya kalır.
- Bayatlık: build betiği önceki `public/meta.json`'daki `generatedAt`'i okur. EPDK çekimi başarısızsa **ve** veri 14 günden eskiyse çıkış kodu 1 olur (fark edelim); daha yeniyse iş başarılı sayılır.
- Sağlamlık: yeni istasyon sayısı < 5000 veya önceki sayının %90'ından azsa yayınlama.
- Ham yanıt: Actions artifact (90 gün) — repoya commit edilmez.
- Çalışma saati: her gece 04:17 (TR) = cron `17 1 * * *` (UTC).
- Snapshot: uygulama reposundaki `scripts/update_snapshot.sh` bu reponun Pages adreslerinden indirir; bu repoda snapshot işi yok.
- `stations.json` yalnızca `stationsVersion` değiştiyse yeniden yazılır; `meta.json` her başarılı çekimde güncellenir.
- `public/` elle düzenlenince (push) workflow `--meta-only` ile sürüm hash'lerini günceller; `generatedAt` değişmez.

## Güvenlik / KVKK
- `sarjIstasyonuIsletmecisi` gerçek kişi adı içerebilir (ilk çekimde ~100 kayıt) → yayınlanan hiçbir dosyaya YAZILMAZ. Çıktı alanları sabit izin listesinden yazılır; testle korunur.
- Ham EPDK yanıtı (`fixtures/`, `raw/`) repoya girmez; Actions'ta 90 günlük artifact olarak saklanır.
- Repoda hiçbir anahtar/şifre yok ve olmayacak.

## Çalışma şekli
- Her görevde: önce kısa plan + etkilenecek dosyalar → onayımı bekle → sonra uygula.
- Tahmin etme, önce raporla. EPDK'ya kota nedeniyle gereksiz istek atma; `--from-raw` kullan.
- Her değişiklikten sonra `python3 scripts/test_build_stations.py` geçmeli. Küçük commit'ler.

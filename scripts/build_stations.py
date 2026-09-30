#!/usr/bin/env python3
"""Prizy veri hattı: EPDK şarj istasyonu listesini yayın formatına çevirir.

Kullanım:
  build_stations.py                      gerçek istek at, public/ altına yayınla
  build_stations.py --save-raw <dosya>   gerçek istek at, ham yanıtı da kaydet
  build_stations.py --from-raw <dosya>   istek atmadan kaydedilmiş yanıttan üret
  build_stations.py --from-raw <dosya> --draft-brands
                                         brands.json taslağı + prices.json iskeleti

Kurallar docs/PLAN.md'de ("Veri formatları", "Veri kuralları", "Veri hattı davranışı").
Sadece Python standart kütüphanesi kullanılır.
"""

import argparse
import collections
import datetime
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request

EPDK_URL = "https://apigateway.epdk.gov.tr/sarjIstasyonlari/"
SOURCE = "EPDK Serbest Erişim Platformu (apigateway.epdk.gov.tr/sarjIstasyonlari)"
TIMEOUT_SECONDS = 180

LAT_MIN, LAT_MAX = 35.0, 43.0
LNG_MIN, LNG_MAX = 25.0, 45.0

MIN_STATIONS = 5000
MIN_RATIO = 0.9
STALE_DAYS = 14
PRICE_DRAFT_BRANDS = 15

DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_PUBLIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public"
)

SOCKET_STANDARDS = {
    "AC_TYPE2": "Type 2",
    "DC_CCS": "CCS",
    "DC_CHADEMO": "CHAdeMO",
}

UNKNOWN_BRAND_SLUG = "bilinmeyen"

_TR_MAP = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iisSgGuUoOcC")


class FetchError(Exception):
    """EPDK çekimi başarısız (429, 5xx, ağ hatası, zaman aşımı)."""


# ---------------------------------------------------------------- çekim

def fetch_raw():
    """EPDK'ya TEK istek atar. Yeniden deneme yok (kota: saatte 1)."""
    request = urllib.request.Request(
        EPDK_URL,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise FetchError("HTTP 429: EPDK kotası aşıldı (saatte 1 istek)")
        raise FetchError("HTTP %s" % error.code)
    except (urllib.error.URLError, OSError) as error:
        raise FetchError("ağ hatası: %s" % error)


def extract_records(raw_bytes):
    """Ham yanıttan kayıt listesini çıkarır; biçim beklenmedikse ValueError."""
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError('yanıtta "data" listesi yok')
    return payload["data"]


# ---------------------------------------------------------------- alan dönüşümleri

def is_public(hizmet_sekli):
    """ "HALKA_ACIK", "Halka Açık" vb. → True."""
    if not isinstance(hizmet_sekli, str):
        return False
    return hizmet_sekli.strip().casefold().startswith("halka")


def parse_number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_kw(value):
    """soketGucu ("180.0", "", None) → sayı ya da None."""
    number = parse_number(value)
    if number is None or number <= 0:
        return None
    number = round(number, 2)
    return int(number) if number == int(number) else number


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in ("true", "evet", "e", "1", "yes")
    return value == 1


def clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def slugify(text):
    text = clean_text(text).translate(_TR_MAP)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def socket_standard(soket_turu):
    key = clean_text(soket_turu).upper()
    if key in SOCKET_STANDARDS:
        return SOCKET_STANDARDS[key], True
    for prefix in ("AC_", "DC_"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    return key, False


def socket_type(soket):
    """(tip, çelişki_var_mı) döner. soketTipi ile soketTuru çelişirse
    soketTuru önekine güvenilir."""
    tipi = clean_text(soket.get("soketTipi")).upper()
    turu = clean_text(soket.get("soketTuru")).upper()
    from_turu = turu[:2] if turu[:3] in ("AC_", "DC_") else None
    if from_turu:
        return from_turu, tipi in ("AC", "DC") and tipi != from_turu
    if tipi in ("AC", "DC"):
        return tipi, False
    return None, False


# ---------------------------------------------------------------- markalar

class BrandIndex(object):
    """brands.json'daki slug + aliases üzerinden ham `marka` → slug.

    Aynı yazımı kullanan farklı operatörler `licenseNos`
    (sarjAgiIsletmecisiLisansNo listesi) ile ayrılır.
    """

    def __init__(self, brands):
        self._by_key = {}
        for brand in brands or []:
            slug = brand.get("slug")
            if not slug:
                continue
            licenses = frozenset(clean_text(no) for no in brand.get("licenseNos") or [])
            for alias in [slug, brand.get("name")] + list(brand.get("aliases") or []):
                key = slugify(alias)
                candidates = self._by_key.setdefault(key, []) if key else None
                if candidates is not None and all(slug != other for other, _ in candidates):
                    candidates.append((slug, licenses))

    def resolve(self, raw_brand, license_no=None):
        """(slug, eşleşti_mi) döner. Eşleşmeyen marka otomatik slug alır."""
        key = slugify(raw_brand)
        if not key:
            return UNKNOWN_BRAND_SLUG, False
        candidates = self._by_key.get(key) or []
        license_no = clean_text(license_no)
        for slug, licenses in candidates:
            if licenses and license_no in licenses:
                return slug, True
        for slug, licenses in candidates:
            if not licenses:
                return slug, True
        return key, False


# ---------------------------------------------------------------- dönüşüm

def new_report():
    return {
        "total": 0,
        "public": 0,
        "published": 0,
        "dropped_not_public": 0,
        "dropped_no_id": 0,
        "dropped_bad_coords": 0,
        "dropped_outside_turkey": 0,
        "dropped_no_sockets": 0,
        "duplicate_ids": {},
        "sockets": 0,
        "sockets_without_kw": 0,
        "sockets_unknown_type": 0,
        "sockets_type_conflict": 0,
        "unknown_standards": {},
        "unmatched_brands": {},
        "brand_operators": {},
    }


def build_sockets(raw_sockets, report):
    sockets = []
    for soket in raw_sockets or []:
        if not isinstance(soket, dict):
            continue
        kind, conflict = socket_type(soket)
        if kind is None:
            report["sockets_unknown_type"] += 1
            continue
        if conflict:
            report["sockets_type_conflict"] += 1
        standard, known = socket_standard(soket.get("soketTuru"))
        if not known:
            raw = clean_text(soket.get("soketTuru"))
            report["unknown_standards"][raw] = report["unknown_standards"].get(raw, 0) + 1
        kw = parse_kw(soket.get("soketGucu"))
        report["sockets"] += 1
        if kw is None:
            report["sockets_without_kw"] += 1
        # soketNo ("SKT/18029") V1'de yayınlanmıyor; ileride canlı doluluk için
        # operatör verisini sokete bağlamanın anahtarı (PLAN.md). Açmak için:
        #   "no": clean_text(soket.get("soketNo")),
        sockets.append({"type": kind, "standard": standard, "kw": kw})
    return sockets


def max_kw(sockets, kind):
    values = [s["kw"] for s in sockets if s["type"] == kind and s["kw"] is not None]
    return max(values) if values else None


def build_station(record, brand_index, report):
    """Tek EPDK kaydını yayın formatına çevirir.

    Çıktı alanları burada tek tek yazılır; `sarjIstasyonuIsletmecisi` (gerçek
    kişi adı içerebilir, KVKK) hiç okunmaz. Soketi olmayan istasyon None döner.
    """
    sockets = build_sockets(record.get("soketler"), report)
    if not sockets:
        return None
    raw_brand = clean_text(record.get("marka"))
    license_no = clean_text(record.get("sarjAgiIsletmecisiLisansNo"))
    slug, matched = brand_index.resolve(raw_brand, license_no)
    if not matched:
        entry = report["unmatched_brands"].setdefault(raw_brand, {"slug": slug, "count": 0})
        entry["count"] += 1
    operators = report["brand_operators"].setdefault(slug, {})
    operators.setdefault(license_no, clean_text(record.get("sarjAgiIsletmecisiUnvan")))
    return {
        "id": clean_text(record.get("sarjIstasyonuNo")),
        "name": clean_text(record.get("sarjIstasyonuAdi")),
        "brand": slug,
        "operator": clean_text(record.get("sarjAgiIsletmecisiUnvan")),
        "address": clean_text(record.get("adres")),
        "lat": parse_number(record.get("enlem")),
        "lng": parse_number(record.get("boylam")),
        "green": parse_bool(record.get("yesilSarjIstasyonuMu")),
        "sockets": sockets,
        "acCount": sum(1 for s in sockets if s["type"] == "AC"),
        "acMaxKw": max_kw(sockets, "AC"),
        "dcCount": sum(1 for s in sockets if s["type"] == "DC"),
        "dcMaxKw": max_kw(sockets, "DC"),
    }


def accept_record(record, report):
    """Kayıt yayınlanacaksa True; değilse nedeni rapora işler."""
    if not isinstance(record, dict) or not is_public(record.get("hizmetSekli")):
        report["dropped_not_public"] += 1
        return False
    report["public"] += 1
    if not clean_text(record.get("sarjIstasyonuNo")):
        report["dropped_no_id"] += 1
        return False
    lat = parse_number(record.get("enlem"))
    lng = parse_number(record.get("boylam"))
    if lat is None or lng is None:
        report["dropped_bad_coords"] += 1
        return False
    if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
        report["dropped_outside_turkey"] += 1
        return False
    return True


def transform(records, brand_index):
    """EPDK kayıtları → (id'ye göre sıralı istasyon listesi, rapor)."""
    report = new_report()
    report["total"] = len(records)
    by_id = {}
    for record in records:
        if not accept_record(record, report):
            continue
        station_id = clean_text(record.get("sarjIstasyonuNo"))
        if station_id in by_id:
            # tekrarlanan id: ilk kayıt kalır
            report["duplicate_ids"][station_id] = report["duplicate_ids"].get(station_id, 0) + 1
            continue
        station = build_station(record, brand_index, report)
        if station is None:
            report["dropped_no_sockets"] += 1
            continue
        by_id[station_id] = station
    stations = [by_id[key] for key in sorted(by_id)]
    report["published"] = len(stations)
    return stations, report


def print_report(report, out=sys.stdout):
    lines = [
        "Toplam kayıt:            %d" % report["total"],
        "Halka açık:              %d" % report["public"],
        "Yayınlanan istasyon:     %d" % report["published"],
        "Atılan (halka açık değil): %d" % report["dropped_not_public"],
        "Atılan (id yok):         %d" % report["dropped_no_id"],
        "Atılan (koordinat bozuk): %d" % report["dropped_bad_coords"],
        "Atılan (Türkiye dışı):   %d" % report["dropped_outside_turkey"],
        "Atılan (soketsiz):       %d" % report["dropped_no_sockets"],
        "Soket:                   %d (gücü boş: %d, tipi tanınmayan: %d, tip/tür çelişkili: %d)"
        % (report["sockets"], report["sockets_without_kw"], report["sockets_unknown_type"],
           report["sockets_type_conflict"]),
    ]
    duplicates = report["duplicate_ids"]
    lines.append(
        "Tekrarlanan id:          %d id, %d fazla kayıt atıldı"
        % (len(duplicates), sum(duplicates.values()))
    )
    for station_id in sorted(duplicates):
        lines.append("  UYARI tekrarlanan id: %s (+%d)" % (station_id, duplicates[station_id]))
    for raw in sorted(report["unknown_standards"]):
        lines.append(
            "  UYARI tanınmayan soketTuru: %r (%d)" % (raw, report["unknown_standards"][raw])
        )
    unmatched = report["unmatched_brands"]
    lines.append("Eşleşmeyen marka:        %d" % len(unmatched))
    for raw in sorted(unmatched, key=lambda k: (-unmatched[k]["count"], k)):
        lines.append(
            "  UYARI eşleşmeyen marka: %r → %s (%d istasyon)"
            % (raw, unmatched[raw]["slug"], unmatched[raw]["count"])
        )
    shared = dict((slug, ops) for slug, ops in report["brand_operators"].items() if len(ops) > 1)
    lines.append("Birden çok operatörlü slug: %d" % len(shared))
    for slug in sorted(shared):
        for license_no in sorted(shared[slug]):
            lines.append("  UYARI aynı slug, farklı operatör: %s ← %s (%s)"
                         % (slug, license_no, shared[slug][license_no]))
    out.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- sürüm, sağlamlık, bayatlık

def short_hash(data):
    return hashlib.sha256(data).hexdigest()[:12]


def stations_version(stations):
    """Sadece istasyon listesinden hesaplanır; generatedAt hash'e girmez."""
    canonical = json.dumps(stations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return short_hash(canonical.encode("utf-8"))


def file_version(path):
    try:
        with open(path, "rb") as handle:
            return short_hash(handle.read())
    except OSError:
        return None


def health_problem(count, previous_count, min_stations=MIN_STATIONS):
    """Yayına engel bir sorun varsa açıklamasını, yoksa None döner."""
    if count < min_stations:
        return "istasyon sayısı %d < %d" % (count, min_stations)
    if previous_count and count < previous_count * MIN_RATIO:
        return "istasyon sayısı %d, önceki %d sayısının %%%d'ından az" % (
            count, previous_count, int(MIN_RATIO * 100))
    return None


def is_stale(previous_meta, now):
    """Yayındaki veri STALE_DAYS günden eski mi? Önceki meta yoksa eski sayılır."""
    try:
        generated = datetime.datetime.strptime(previous_meta["generatedAt"], DATE_FORMAT)
    except (TypeError, KeyError, ValueError):
        return True
    return now - generated > datetime.timedelta(days=STALE_DAYS)


# ---------------------------------------------------------------- dosya yazımı

def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_text(path, text):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def dump_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_list(items):
    """Her öğe tek satırda: günlük diff okunur, dosya küçük kalır."""
    if not items:
        return "[]"
    return "[\n" + ",\n".join(dump_line(item) for item in items) + "\n]"


def render_stations(stations, generated_at):
    return '{\n"generatedAt":%s,\n"source":%s,\n"stations":%s\n}\n' % (
        dump_line(generated_at), dump_line(SOURCE), render_list(stations))


def build_meta(public_dir, stations, generated_at):
    prices = read_json(os.path.join(public_dir, "prices.json"), {})
    return {
        "generatedAt": generated_at,
        "stationsVersion": stations_version(stations),
        "pricesVersion": file_version(os.path.join(public_dir, "prices.json")),
        "brandsVersion": file_version(os.path.join(public_dir, "brands.json")),
        "vehiclesVersion": file_version(os.path.join(public_dir, "vehicles.json")),
        "pricesUpdatedAt": prices.get("updatedAt") if isinstance(prices, dict) else None,
        "stationCount": len(stations),
    }


def publish(public_dir, stations, now):
    generated_at = now.strftime(DATE_FORMAT)
    write_text(os.path.join(public_dir, "stations.json"), render_stations(stations, generated_at))
    meta = build_meta(public_dir, stations, generated_at)
    write_text(os.path.join(public_dir, "meta.json"), json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


# ---------------------------------------------------------------- marka / fiyat taslağı

def empty_tariffs():
    return [
        {"type": "AC", "label": "AC", "pricePerKwh": None},
        {"type": "DC", "label": "DC ≤ 100 kW", "maxKw": 100, "pricePerKwh": None},
        {"type": "DC", "label": "DC > 100 kW", "minKw": 100, "pricePerKwh": None},
    ]


def draft_brands(records, existing_brands):
    """Ham `marka` yazımlarından brands.json taslağı.

    Var olan girdiler korunur (elle düzenlemeler ezilmez); yeni yazımlar ilgili
    markanın aliases listesine, yeni markalar listeye eklenir.
    (taslak, slug → istasyon sayısı) döner.
    """
    brands = [dict(brand) for brand in existing_brands or []]
    index = BrandIndex(brands)
    by_slug = dict((brand["slug"], brand) for brand in brands if brand.get("slug"))
    spellings = {}
    counts = collections.Counter()
    for record in records:
        if not isinstance(record, dict) or not is_public(record.get("hizmetSekli")):
            continue
        raw = clean_text(record.get("marka"))
        slug, _ = index.resolve(raw, record.get("sarjAgiIsletmecisiLisansNo"))
        counts[slug] += 1
        if raw:
            spellings.setdefault(slug, collections.Counter())[raw] += 1
    for slug in sorted(counts):
        seen = spellings.get(slug, collections.Counter())
        brand = by_slug.get(slug)
        if brand is None:
            name = seen.most_common(1)[0][0] if seen else slug
            brand = {"slug": slug, "name": name, "aliases": [],
                     "appStore": "", "playStore": "", "color": ""}
            by_slug[slug] = brand
            brands.append(brand)
        brand["aliases"] = sorted(set(brand.get("aliases") or []) | set(seen))
    brands.sort(key=lambda brand: brand.get("slug") or "")
    return brands, counts


def draft_prices(existing_prices, counts, limit=PRICE_DRAFT_BRANDS):
    """İstasyon sayısına göre ilk `limit` marka için fiyatı null iskelet ekler."""
    prices = existing_prices if isinstance(existing_prices, dict) else {}
    prices.setdefault("updatedAt", None)
    brands = prices.setdefault("brands", {})
    known = [slug for slug in counts if slug != UNKNOWN_BRAND_SLUG]
    top = sorted(known, key=lambda slug: (-counts[slug], slug))[:limit]
    for slug in top:
        if slug in brands:
            continue
        brands[slug] = {"tariffs": empty_tariffs(), "sourceUrl": "", "checkedAt": None, "note": ""}
    return prices, top


def run_draft(records, public_dir, out):
    brands_path = os.path.join(public_dir, "brands.json")
    prices_path = os.path.join(public_dir, "prices.json")
    brands, counts = draft_brands(records, read_json(brands_path, []))
    prices, top = draft_prices(read_json(prices_path, {}), counts)
    write_text(brands_path, render_list(brands) + "\n")
    write_text(prices_path, json.dumps(prices, ensure_ascii=False, indent=2) + "\n")
    out.write("brands.json taslağı: %d marka\n" % len(brands))
    for slug in sorted(counts, key=lambda s: (-counts[s], s)):
        aliases = [b for b in brands if b["slug"] == slug][0]["aliases"]
        out.write("  %-28s %6d istasyon  yazımlar: %s\n" % (slug, counts[slug], aliases))
    out.write("prices.json iskeleti (ilk %d): %s\n" % (len(top), ", ".join(top)))
    _, report = transform(records, BrandIndex(brands))
    shared = dict((slug, ops) for slug, ops in report["brand_operators"].items() if len(ops) > 1)
    out.write("Birden çok operatörlü slug (licenseNos ile ayırmayı düşün): %d\n" % len(shared))
    for slug in sorted(shared):
        for license_no in sorted(shared[slug]):
            out.write("  %s ← %s (%s)\n" % (slug, license_no, shared[slug][license_no]))


# ---------------------------------------------------------------- ana akış

def parse_args(argv):
    parser = argparse.ArgumentParser(description="EPDK şarj istasyonu verisini yayın formatına çevirir.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--from-raw", metavar="DOSYA", help="istek atmadan kaydedilmiş ham yanıttan üret")
    source.add_argument("--save-raw", metavar="DOSYA", help="gerçek istek at, ham yanıtı bu dosyaya kaydet")
    parser.add_argument("--public-dir", default=DEFAULT_PUBLIC_DIR, help="yayın klasörü (varsayılan: data/public)")
    parser.add_argument("--draft-brands", action="store_true",
                        help="stations/meta yazmadan brands.json taslağı ve prices.json iskeleti üret")
    parser.add_argument("--min-stations", type=int, default=MIN_STATIONS,
                        help="bu sayının altında yayınlama (varsayılan: %d)" % MIN_STATIONS)
    return parser.parse_args(argv)


def main(argv=None, now=None, fetch=None, out=sys.stdout):
    args = parse_args(argv)
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    fetch = fetch or fetch_raw
    public_dir = args.public_dir
    previous_meta = read_json(os.path.join(public_dir, "meta.json"))

    def fetch_failed(reason):
        out.write("EPDK çekimi başarısız: %s. Yayınlanmadı, önceki dosyalar duruyor.\n" % reason)
        if is_stale(previous_meta, now):
            out.write("HATA: yayındaki veri %d günden eski (ya da hiç yok).\n" % STALE_DAYS)
            return 1
        return 0

    if args.from_raw:
        try:
            with open(args.from_raw, "rb") as handle:
                raw = handle.read()
            records = extract_records(raw)
        except (OSError, ValueError) as error:
            out.write("HATA: %s okunamadı: %s\n" % (args.from_raw, error))
            return 1
    else:
        try:
            raw = fetch()
        except FetchError as error:
            return fetch_failed(str(error))
        if args.save_raw:
            directory = os.path.dirname(args.save_raw)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.save_raw, "wb") as handle:
                handle.write(raw)
            out.write("Ham yanıt kaydedildi: %s (%d bayt)\n" % (args.save_raw, len(raw)))
        try:
            records = extract_records(raw)
        except ValueError as error:
            return fetch_failed("yanıt okunamadı (%s)" % error)

    if args.draft_brands:
        run_draft(records, public_dir, out)
        return 0

    brand_index = BrandIndex(read_json(os.path.join(public_dir, "brands.json"), []))
    stations, report = transform(records, brand_index)
    print_report(report, out)

    previous_count = (previous_meta or {}).get("stationCount")
    problem = health_problem(len(stations), previous_count, args.min_stations)
    if problem:
        out.write("HATA: sağlamlık kontrolü geçmedi (%s). Yayınlanmadı.\n" % problem)
        return 1

    meta = publish(public_dir, stations, now)
    out.write("Yayınlandı: %d istasyon, stationsVersion=%s\n" % (meta["stationCount"], meta["stationsVersion"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

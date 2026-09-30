#!/usr/bin/env python3
"""build_stations.py birim testleri. Gerçek EPDK isteği ATMAZ.

Çalıştırma: python3 data/scripts/test_build_stations.py
"""

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_stations as bs  # noqa: E402

NOW = datetime.datetime(2026, 10, 1, 1, 17, 0)


def record(no, **overrides):
    """Sahte EPDK kaydı; alan adları CLAUDE.md'deki doğrulanmış şemadan."""
    base = {
        "sarjIstasyonuNo": no,
        "sarjIstasyonuAdi": "İstasyon %s" % no,
        "yesilSarjIstasyonuMu": False,
        "hizmetSekli": "HALKA_ACIK",
        "sarjAgiIsletmecisiUnvan": "ZES DİJİTAL TİCARET A.Ş.",
        "sarjAgiIsletmecisiLisansNo": "ŞA/1",
        "sarjIstasyonuIsletmecisi": "AHMET YILMAZ",
        "marka": "ZES",
        "soketler": [
            {"soketTipi": "DC", "soketTuru": "DC_CCS", "soketGucu": "180.0"},
            {"soketTipi": "AC", "soketTuru": "AC_TYPE2", "soketGucu": "22.0"},
        ],
        "adres": "Örnek Mah. 1. Sok. İstanbul",
        "enlem": 41.0,
        "boylam": 29.0,
    }
    base.update(overrides)
    return base


def raw_bytes(records):
    return json.dumps({"data": records}, ensure_ascii=False).encode("utf-8")


def run_transform(records, brands=None):
    return bs.transform(records, bs.BrandIndex(brands or []))


class PublicFilterTest(unittest.TestCase):
    def test_spelling_variants(self):
        for value in ("HALKA_ACIK", "Halka Açık", "halka açık", "  HALKA AÇIK ", "Halka_Acik"):
            self.assertTrue(bs.is_public(value), value)

    def test_non_public(self):
        for value in ("OZEL", "Özel", "", None, 1, "Yarı Halka Açık"):
            self.assertFalse(bs.is_public(value), value)

    def test_only_public_published(self):
        stations, report = run_transform([
            record("ŞRJ/1"),
            record("ŞRJ/2", hizmetSekli="OZEL"),
            record("ŞRJ/3", hizmetSekli="Halka Açık"),
        ])
        self.assertEqual([s["id"] for s in stations], ["ŞRJ/1", "ŞRJ/3"])
        self.assertEqual(report["public"], 2)
        self.assertEqual(report["dropped_not_public"], 1)


class TurkeyBoxTest(unittest.TestCase):
    def test_outside_and_bad_coordinates_dropped(self):
        stations, report = run_transform([
            record("ŞRJ/1", enlem=41.0, boylam=29.0),
            record("ŞRJ/2", enlem=52.5, boylam=13.4),      # Berlin
            record("ŞRJ/3", enlem=29.0, boylam=41.0),      # enlem/boylam ters
            record("ŞRJ/4", enlem=0, boylam=0),
            record("ŞRJ/5", enlem=None, boylam=29.0),
            record("ŞRJ/6", enlem="abc", boylam="29"),
            record("ŞRJ/7", enlem="38,5", boylam="27.1"),  # string + virgül
        ])
        self.assertEqual([s["id"] for s in stations], ["ŞRJ/1", "ŞRJ/7"])
        self.assertEqual(report["dropped_outside_turkey"], 3)
        self.assertEqual(report["dropped_bad_coords"], 2)
        self.assertEqual((stations[1]["lat"], stations[1]["lng"]), (38.5, 27.1))

    def test_box_edges_inclusive(self):
        stations, _ = run_transform([
            record("ŞRJ/1", enlem=35.0, boylam=25.0),
            record("ŞRJ/2", enlem=43.0, boylam=45.0),
            record("ŞRJ/3", enlem=43.01, boylam=45.0),
        ])
        self.assertEqual(len(stations), 2)


class SocketTest(unittest.TestCase):
    def test_parse_kw(self):
        self.assertEqual(bs.parse_kw("180.0"), 180)
        self.assertIsInstance(bs.parse_kw("180.0"), int)
        self.assertEqual(bs.parse_kw("7.4"), 7.4)
        self.assertEqual(bs.parse_kw("22,5"), 22.5)
        self.assertEqual(bs.parse_kw(60), 60)
        for empty in ("", "  ", None, "yok", "0", "-5", "nan"):
            self.assertIsNone(bs.parse_kw(empty), repr(empty))

    def test_empty_power_is_null_and_excluded_from_max(self):
        stations, report = run_transform([record("ŞRJ/1", soketler=[
            {"soketTipi": "DC", "soketTuru": "DC_CCS", "soketGucu": ""},
            {"soketTipi": "DC", "soketTuru": "DC_CHADEMO", "soketGucu": "60.0"},
            {"soketTipi": "AC", "soketTuru": "AC_TYPE2", "soketGucu": None},
        ])])
        station = stations[0]
        self.assertEqual(station["sockets"], [
            {"type": "DC", "standard": "CCS", "kw": None},
            {"type": "DC", "standard": "CHAdeMO", "kw": 60},
            {"type": "AC", "standard": "Type 2", "kw": None},
        ])
        self.assertEqual((station["dcCount"], station["dcMaxKw"]), (2, 60))
        self.assertEqual((station["acCount"], station["acMaxKw"]), (1, None))
        self.assertEqual(report["sockets_without_kw"], 2)

    def test_station_without_sockets_dropped(self):
        unusable = [{"soketTipi": "", "soketTuru": "", "soketGucu": "11"}]
        stations, report = run_transform([
            record("ŞRJ/1", soketler=[]),
            record("ŞRJ/2", soketler=None),
            record("ŞRJ/3", soketler=unusable),
            record("ŞRJ/4"),
        ])
        self.assertEqual([s["id"] for s in stations], ["ŞRJ/4"])
        self.assertEqual(report["dropped_no_sockets"], 3)
        self.assertEqual(list(report["brand_operators"]), ["zes"])

    def test_type_conflict_trusts_soket_turu(self):
        stations, report = run_transform([record("ŞRJ/1", soketler=[
            {"soketTipi": "DC", "soketTuru": "AC_TYPE2", "soketGucu": "22"},
            {"soketTipi": "AC", "soketTuru": "DC_CCS", "soketGucu": "120"},
            {"soketTipi": "AC", "soketTuru": "AC_TYPE2", "soketGucu": "11"},
        ])])
        self.assertEqual([(s["type"], s["standard"]) for s in stations[0]["sockets"]],
                         [("AC", "Type 2"), ("DC", "CCS"), ("AC", "Type 2")])
        self.assertEqual((stations[0]["acCount"], stations[0]["acMaxKw"]), (2, 22))
        self.assertEqual(report["sockets_type_conflict"], 2)

    def test_soket_no_not_published(self):
        stations, _ = run_transform([record("ŞRJ/1", soketler=[
            {"soketNo": "SKT/1", "soketTipi": "AC", "soketTuru": "AC_TYPE2", "soketGucu": "22"}])])
        self.assertEqual(set(stations[0]["sockets"][0]), {"type", "standard", "kw"})

    def test_unknown_standard_reported(self):
        stations, report = run_transform([record("ŞRJ/1", soketler=[
            {"soketTipi": "DC", "soketTuru": "DC_GBT", "soketGucu": "120"},
            {"soketTipi": "", "soketTuru": "AC_TYPE2", "soketGucu": "11"},
            {"soketTipi": "", "soketTuru": "", "soketGucu": "11"},
            {"soketTipi": "AC", "soketTuru": "TESLA", "soketGucu": "11"},
        ])])
        self.assertEqual(stations[0]["sockets"], [
            {"type": "DC", "standard": "GBT", "kw": 120},
            {"type": "AC", "standard": "Type 2", "kw": 11},
            {"type": "AC", "standard": "TESLA", "kw": 11},
        ])
        self.assertEqual(report["unknown_standards"], {"DC_GBT": 1, "TESLA": 1})
        self.assertEqual(report["sockets_unknown_type"], 1)


class DuplicateIdTest(unittest.TestCase):
    def test_first_record_kept_and_reported(self):
        stations, report = run_transform([
            record("ŞRJ/2", sarjIstasyonuAdi="ilk"),
            record("ŞRJ/1"),
            record("ŞRJ/2", sarjIstasyonuAdi="ikinci"),
            record(" ŞRJ/2 ", sarjIstasyonuAdi="üçüncü"),
        ])
        self.assertEqual([s["id"] for s in stations], ["ŞRJ/1", "ŞRJ/2"])
        self.assertEqual(stations[1]["name"], "ilk")
        self.assertEqual(report["duplicate_ids"], {"ŞRJ/2": 2})
        out = io.StringIO()
        bs.print_report(report, out)
        self.assertIn("UYARI tekrarlanan id: ŞRJ/2 (+2)", out.getvalue())

    def test_missing_id_dropped(self):
        stations, report = run_transform([record(""), record(None), record("ŞRJ/1")])
        self.assertEqual(len(stations), 1)
        self.assertEqual(report["dropped_no_id"], 2)

    def test_sorted_by_id(self):
        stations, _ = run_transform([record("ŞRJ/30"), record("ŞRJ/10"), record("ŞRJ/20")])
        self.assertEqual([s["id"] for s in stations], ["ŞRJ/10", "ŞRJ/20", "ŞRJ/30"])


class KvkkTest(unittest.TestCase):
    ALLOWED = {"id", "name", "brand", "operator", "address", "lat", "lng", "green",
               "sockets", "acCount", "acMaxKw", "dcCount", "dcMaxKw"}

    def test_station_fields_are_allow_listed(self):
        stations, _ = run_transform([record("ŞRJ/1", yeniBilinmeyenAlan="x")])
        self.assertEqual(set(stations[0]), self.ALLOWED)
        self.assertEqual(stations[0]["operator"], "ZES DİJİTAL TİCARET A.Ş.")

    def test_person_name_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "raw.json")
            with open(raw, "wb") as handle:
                handle.write(raw_bytes([record("ŞRJ/1"), record("ŞRJ/2")]))
            code = bs.main(["--from-raw", raw, "--public-dir", tmp, "--min-stations", "1"],
                           now=NOW, out=io.StringIO())
            self.assertEqual(code, 0)
            for name in ("stations.json", "meta.json"):
                with open(os.path.join(tmp, name), encoding="utf-8") as handle:
                    text = handle.read()
                self.assertNotIn("AHMET YILMAZ", text)
                self.assertNotIn("sarjIstasyonuIsletmecisi", text)


class BrandTest(unittest.TestCase):
    BRANDS = [{"slug": "zes", "name": "ZES", "aliases": ["zes", "ZES"]},
              {"slug": "esarj", "name": "Eşarj", "aliases": ["eşarj"]}]

    def test_slugify(self):
        self.assertEqual(bs.slugify("EŞARJ"), "esarj")
        self.assertEqual(bs.slugify("  Şarj  İstasyonu Iğdır "), "sarj-istasyonu-igdir")
        self.assertEqual(bs.slugify("VOLTRUN"), "voltrun")
        self.assertEqual(bs.slugify("On/Off & Go!"), "on-off-go")
        self.assertEqual(bs.slugify(None), "")

    def test_alias_match_and_auto_slug(self):
        stations, report = run_transform([
            record("ŞRJ/1", marka="zes"),
            record("ŞRJ/2", marka=" Zes "),
            record("ŞRJ/3", marka="EŞARJ"),
            record("ŞRJ/4", marka="VOLTRUN"),
            record("ŞRJ/5", marka="Voltrun"),
            record("ŞRJ/6", marka=""),
        ], self.BRANDS)
        self.assertEqual([s["brand"] for s in stations],
                         ["zes", "zes", "esarj", "voltrun", "voltrun", "bilinmeyen"])
        self.assertEqual(report["unmatched_brands"], {
            "VOLTRUN": {"slug": "voltrun", "count": 1},
            "Voltrun": {"slug": "voltrun", "count": 1},
            "": {"slug": "bilinmeyen", "count": 1},
        })

    SPLIT = [
        {"slug": "greenwatt", "name": "GREENWATT", "aliases": ["GREENWATT"], "licenseNos": ["ŞH/1"]},
        {"slug": "greenwatt-gwesi", "name": "Greenwatt", "aliases": ["Greenwatt"], "licenseNos": ["ŞH/2"]},
    ]

    def test_same_spelling_split_by_operator_license(self):
        stations, report = run_transform([
            record("ŞRJ/1", marka="GREENWATT", sarjAgiIsletmecisiLisansNo="ŞH/1"),
            record("ŞRJ/2", marka="Greenwatt", sarjAgiIsletmecisiLisansNo="ŞH/2"),
            record("ŞRJ/3", marka="GREENWATT", sarjAgiIsletmecisiLisansNo="ŞH/2"),
            record("ŞRJ/4", marka="greenwatt", sarjAgiIsletmecisiLisansNo="ŞH/9"),
        ], self.SPLIT)
        self.assertEqual([s["brand"] for s in stations],
                         ["greenwatt", "greenwatt-gwesi", "greenwatt-gwesi", "greenwatt"])
        # lisansı tanınmayan üçüncü operatör eşleşmedi sayılır ve raporlanır
        self.assertEqual(report["unmatched_brands"], {"greenwatt": {"slug": "greenwatt", "count": 1}})
        out = io.StringIO()
        bs.print_report(report, out)
        self.assertIn("Birden çok operatörlü slug: 1", out.getvalue())
        self.assertIn("UYARI aynı slug, farklı operatör: greenwatt ← ŞH/9", out.getvalue())

    def test_multi_operator_slug_reported(self):
        _, report = run_transform([
            record("ŞRJ/1", marka="X", sarjAgiIsletmecisiLisansNo="ŞH/1", sarjAgiIsletmecisiUnvan="A A.Ş."),
            record("ŞRJ/2", marka="x", sarjAgiIsletmecisiLisansNo="ŞH/2", sarjAgiIsletmecisiUnvan="B A.Ş."),
            record("ŞRJ/3", marka="Y", sarjAgiIsletmecisiLisansNo="ŞH/2", sarjAgiIsletmecisiUnvan="B A.Ş."),
        ])
        self.assertEqual(report["brand_operators"]["x"], {"ŞH/1": "A A.Ş.", "ŞH/2": "B A.Ş."})
        self.assertEqual(report["brand_operators"]["y"], {"ŞH/2": "B A.Ş."})

    def test_draft_respects_license_split(self):
        records = [
            record("ŞRJ/1", marka="GREENWATT", sarjAgiIsletmecisiLisansNo="ŞH/1"),
            record("ŞRJ/2", marka="Greenwatt", sarjAgiIsletmecisiLisansNo="ŞH/2"),
        ]
        brands, counts = bs.draft_brands(records, self.SPLIT)
        self.assertEqual([b["slug"] for b in brands], ["greenwatt", "greenwatt-gwesi"])
        self.assertEqual(dict(counts), {"greenwatt": 1, "greenwatt-gwesi": 1})
        self.assertEqual(brands[1]["licenseNos"], ["ŞH/2"])

    def test_draft_brands_groups_spellings_and_keeps_manual_edits(self):
        existing = [{"slug": "zes", "name": "ZES", "aliases": ["ZES"],
                     "appStore": "https://apps.example/zes", "playStore": "", "color": "#00f"}]
        records = [
            record("ŞRJ/1", marka="zes"), record("ŞRJ/2", marka="ZES"),
            record("ŞRJ/3", marka="Voltrun"), record("ŞRJ/4", marka="VOLTRUN"),
            record("ŞRJ/5", marka="VOLTRUN"),
            record("ŞRJ/6", marka="Özel Marka", hizmetSekli="OZEL"),
        ]
        brands, counts = bs.draft_brands(records, existing)
        self.assertEqual([b["slug"] for b in brands], ["voltrun", "zes"])
        self.assertEqual(brands[0], {"slug": "voltrun", "name": "VOLTRUN",
                                     "aliases": ["VOLTRUN", "Voltrun"],
                                     "appStore": "", "playStore": "", "color": ""})
        self.assertEqual(brands[1]["aliases"], ["ZES", "zes"])
        self.assertEqual(brands[1]["appStore"], "https://apps.example/zes")
        self.assertEqual(dict(counts), {"zes": 2, "voltrun": 3})

    def test_draft_prices_top_n_null_and_keeps_existing(self):
        existing = {"updatedAt": "2026-09-30", "brands": {"zes": {"tariffs": [
            {"type": "AC", "label": "AC", "pricePerKwh": 9.99}], "sourceUrl": "x",
            "checkedAt": "2026-09-30", "note": ""}}}
        counts = {"zes": 50, "voltrun": 30, "kucuk": 1, "bilinmeyen": 99}
        prices, top = bs.draft_prices(existing, counts, limit=2)
        self.assertEqual(top, ["zes", "voltrun"])
        self.assertEqual(sorted(prices["brands"]), ["voltrun", "zes"])
        self.assertEqual(prices["brands"]["zes"]["tariffs"][0]["pricePerKwh"], 9.99)
        tariffs = prices["brands"]["voltrun"]["tariffs"]
        self.assertEqual(len(tariffs), 3)
        self.assertTrue(all(t["pricePerKwh"] is None for t in tariffs))


class HealthTest(unittest.TestCase):
    def test_minimum_count(self):
        self.assertIsNotNone(bs.health_problem(4999, None))
        self.assertIsNone(bs.health_problem(5000, None))

    def test_ninety_percent_of_previous(self):
        self.assertIsNotNone(bs.health_problem(11699, 13000))
        self.assertIsNone(bs.health_problem(11700, 13000))
        self.assertIsNone(bs.health_problem(14000, 13000))

    def test_failed_health_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "raw.json")
            with open(raw, "wb") as handle:
                handle.write(raw_bytes([record("ŞRJ/1")]))
            out = io.StringIO()
            code = bs.main(["--from-raw", raw, "--public-dir", tmp], now=NOW, out=out)
            self.assertEqual(code, 1)
            self.assertIn("sağlamlık", out.getvalue())
            self.assertFalse(os.path.exists(os.path.join(tmp, "stations.json")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "meta.json")))

    def test_drop_against_previous_keeps_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_meta = {"generatedAt": "2026-09-30T01:17:00Z", "stationCount": 10}
            with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as handle:
                json.dump(old_meta, handle)
            raw = os.path.join(tmp, "raw.json")
            with open(raw, "wb") as handle:
                handle.write(raw_bytes([record("ŞRJ/%d" % i) for i in range(8)]))
            code = bs.main(["--from-raw", raw, "--public-dir", tmp, "--min-stations", "1"],
                           now=NOW, out=io.StringIO())
            self.assertEqual(code, 1)
            self.assertEqual(bs.read_json(os.path.join(tmp, "meta.json")), old_meta)


class StalenessTest(unittest.TestCase):
    def failing_fetch(self):
        self.calls += 1
        raise bs.FetchError("HTTP 429: EPDK kotası aşıldı")

    def run_failed_fetch(self, generated_at):
        self.calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            if generated_at:
                with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as handle:
                    json.dump({"generatedAt": generated_at, "stationCount": 13000}, handle)
            out = io.StringIO()
            code = bs.main(["--public-dir", tmp], now=NOW, fetch=self.failing_fetch, out=out)
            self.assertFalse(os.path.exists(os.path.join(tmp, "stations.json")))
        return code, out.getvalue()

    def test_fresh_data_exit_zero(self):
        code, _ = self.run_failed_fetch("2026-09-30T01:17:00Z")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, 1)  # yeniden deneme yok

    def test_exactly_fourteen_days_is_not_stale(self):
        code, _ = self.run_failed_fetch("2026-09-17T01:17:00Z")
        self.assertEqual(code, 0)

    def test_older_than_fourteen_days_exit_one(self):
        code, text = self.run_failed_fetch("2026-09-17T01:16:59Z")
        self.assertEqual(code, 1)
        self.assertIn("14 günden eski", text)

    def test_no_previous_meta_exit_one(self):
        code, _ = self.run_failed_fetch(None)
        self.assertEqual(code, 1)

    def test_unreadable_response_counts_as_failed_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fault = b'{"fault":{"faultString":"QUOTA"}}'
            code = bs.main(["--public-dir", tmp], now=NOW, fetch=lambda: fault, out=io.StringIO())
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(tmp, "stations.json")))


class FetchTest(unittest.TestCase):
    def test_request_shape(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":[]}'
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(bs.fetch_raw(), b'{"data":[]}')
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://apigateway.epdk.gov.tr/sarjIstasyonlari/")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.data, b"{}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(urlopen.call_args[1]["timeout"], 180)

    def test_429_raises_without_retry(self):
        error = urllib.error.HTTPError(bs.EPDK_URL, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=error) as urlopen:
            with self.assertRaises(bs.FetchError) as caught:
                bs.fetch_raw()
        self.assertIn("429", str(caught.exception))
        self.assertEqual(urlopen.call_count, 1)

    def test_5xx_and_network_errors(self):
        errors = [
            urllib.error.HTTPError(bs.EPDK_URL, 503, "x", {}, io.BytesIO(b"")),
            urllib.error.URLError("dns"),
            TimeoutError("timed out"),
        ]
        for error in errors:
            with mock.patch("urllib.request.urlopen", side_effect=error) as urlopen:
                with self.assertRaises(bs.FetchError):
                    bs.fetch_raw()
            self.assertEqual(urlopen.call_count, 1)


class PublishTest(unittest.TestCase):
    def publish(self, tmp, records, now=NOW, extra=()):
        raw = os.path.join(tmp, "raw.json")
        with open(raw, "wb") as handle:
            handle.write(raw_bytes(records))
        code = bs.main(["--from-raw", raw, "--public-dir", tmp, "--min-stations", "1"] + list(extra),
                       now=now, out=io.StringIO())
        self.assertEqual(code, 0)
        return bs.read_json(os.path.join(tmp, "meta.json"))

    def test_meta_fields_and_stations_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "prices.json"), "w", encoding="utf-8") as handle:
                json.dump({"updatedAt": "2026-09-30", "brands": {}}, handle)
            with open(os.path.join(tmp, "brands.json"), "w", encoding="utf-8") as handle:
                handle.write("[]\n")
            meta = self.publish(tmp, [record("ŞRJ/2"), record("ŞRJ/1")])
            self.assertEqual(set(meta), {"generatedAt", "stationsVersion", "pricesVersion",
                                         "brandsVersion", "vehiclesVersion",
                                         "pricesUpdatedAt", "stationCount"})
            self.assertEqual(meta["generatedAt"], "2026-10-01T01:17:00Z")
            self.assertEqual(meta["stationCount"], 2)
            self.assertEqual(meta["pricesUpdatedAt"], "2026-09-30")
            self.assertRegex(meta["stationsVersion"], r"^[0-9a-f]{12}$")
            self.assertRegex(meta["pricesVersion"], r"^[0-9a-f]{12}$")
            self.assertRegex(meta["brandsVersion"], r"^[0-9a-f]{12}$")
            self.assertIsNone(meta["vehiclesVersion"])  # dosya yok

            published = bs.read_json(os.path.join(tmp, "stations.json"))
            self.assertEqual(published["generatedAt"], meta["generatedAt"])
            self.assertEqual(published["source"], bs.SOURCE)
            self.assertEqual([s["id"] for s in published["stations"]], ["ŞRJ/1", "ŞRJ/2"])
            self.assertEqual(published["stations"][0]["sockets"][0],
                             {"type": "DC", "standard": "CCS", "kw": 180})
            with open(os.path.join(tmp, "stations.json"), encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            self.assertEqual(sum(1 for line in lines if line.startswith('{"id"')), 2)

    def test_stations_version_ignores_generated_at_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.publish(tmp, [record("ŞRJ/1"), record("ŞRJ/2")])
            later = NOW + datetime.timedelta(days=1)
            second = self.publish(tmp, [record("ŞRJ/2"), record("ŞRJ/1")], now=later)
            self.assertNotEqual(first["generatedAt"], second["generatedAt"])
            self.assertEqual(first["stationsVersion"], second["stationsVersion"])
            third = self.publish(tmp, [record("ŞRJ/1"), record("ŞRJ/2", adres="Yeni adres")], now=later)
            self.assertNotEqual(second["stationsVersion"], third["stationsVersion"])

    def test_each_file_has_its_own_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (("prices.json", '{"updatedAt":null,"brands":{}}'),
                               ("brands.json", "[]"), ("vehicles.json", "[]")):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                    handle.write(text)
            before = self.publish(tmp, [record("ŞRJ/1")])
            with open(os.path.join(tmp, "vehicles.json"), "w", encoding="utf-8") as handle:
                handle.write('[{"slug":"togg","name":"Togg"}]')
            after = self.publish(tmp, [record("ŞRJ/1")])
            self.assertNotEqual(before["vehiclesVersion"], after["vehiclesVersion"])
            for key in ("stationsVersion", "pricesVersion", "brandsVersion"):
                self.assertEqual(before[key], after[key], key)
            self.assertIsNone(after["pricesUpdatedAt"])

    def test_save_raw_writes_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = raw_bytes([record("ŞRJ/1")])
            target = os.path.join(tmp, "fixtures", "epdk_raw.json")
            code = bs.main(["--save-raw", target, "--public-dir", tmp, "--min-stations", "1"],
                           now=NOW, fetch=lambda: body, out=io.StringIO())
            self.assertEqual(code, 0)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), body)

    def test_draft_brands_writes_only_brands_and_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "raw.json")
            with open(raw, "wb") as handle:
                handle.write(raw_bytes([record("ŞRJ/1", marka="zes"), record("ŞRJ/2", marka="ZES")]))
            code = bs.main(["--from-raw", raw, "--public-dir", tmp, "--draft-brands"],
                           now=NOW, out=io.StringIO())
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "stations.json")))
            brands = bs.read_json(os.path.join(tmp, "brands.json"))
            self.assertEqual([(b["slug"], b["aliases"]) for b in brands], [("zes", ["ZES", "zes"])])
            prices = bs.read_json(os.path.join(tmp, "prices.json"))
            self.assertEqual(list(prices["brands"]), ["zes"])
            self.assertIsNone(prices["updatedAt"])

    def test_bad_raw_file_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "raw.json")
            with open(raw, "w", encoding="utf-8") as handle:
                handle.write("bozuk")
            self.assertEqual(bs.main(["--from-raw", raw, "--public-dir", tmp], now=NOW, out=io.StringIO()), 1)
            self.assertEqual(bs.main(["--from-raw", raw + ".yok", "--public-dir", tmp], now=NOW, out=io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()

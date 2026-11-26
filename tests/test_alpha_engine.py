import os
import json
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import ideaengine as engine


class TickerMatchingTests(unittest.TestCase):
    def test_longest_company_name_and_unique_suffix_aliases(self):
        directory = (
            "Symbol|Security Name|Test Issue\n"
            "GM|General Motors Corporation|N\n"
            "G|General Inc.|N\n"
            "META|Meta Platforms Inc.|N\n"
            "AH|Alpha Holdings Inc.|N\n"
            "AT|Alpha Technologies Inc.|N\n"
            "File Creation Time: x\n"
        ).encode()
        with patch.object(engine.requests, "get") as get:
            get.return_value.content = directory
            mapping = engine.build_ticker_map()

        self.assertEqual(list(mapping), sorted(mapping, key=lambda value: (-len(value), value)))
        with patch.object(engine, "GLOBAL_TICKER_MAP", mapping):
            self.assertEqual(engine.find_ticker_in_headline("General Motors raises guidance"), "GM")
            self.assertEqual(engine.find_ticker_in_headline("Meta Platforms expands"), "META")
        self.assertNotIn("ALPHA", mapping)

    def test_generic_headlines_do_not_fuzzy_match_directional_playbooks(self):
        headlines = [
            "Lennar Falls More Steeply Than Broader Market",
            "Intuit Posts Strong Earnings Amid Sector-Wide AI Concerns",
            "Marvell Did These Things Right to Send Shares 16% Higher",
            "Bristol-Myers Squibb Phase III Survival Win",
            "First Solar Price Target Cut",
            "TXO Partners LP Declines More Than Market",
        ]
        for headline in headlines:
            with self.subTest(headline=headline):
                event_key, config, match_type = engine.check_all_playbooks({"title": headline})
                self.assertIsNone(event_key)
                self.assertIsNone(config)
                self.assertIsNone(match_type)

    def test_keyword_fallback_still_matches_exact_playbook_phrases(self):
        event_key, config = engine.check_keyword_fallback(
            "Company files for Chapter 11 bankruptcy"
        )
        self.assertEqual(event_key, "Bankruptcy")
        self.assertEqual(config["direction"], "BEARISH")

    def test_negated_guidance_cut_is_neutral(self):
        result = engine._local_semantic_extraction(
            "CEO rules out guidance cut",
            {"event_key": "Guidance_Cut", "direction": "BEARISH"},
        )
        self.assertEqual(result["direction"], "NEUTRAL")
        self.assertTrue(result["is_negated_or_rumor"])


class CalendarTests(unittest.TestCase):
    def test_market_open_schedules_skip_holiday_and_weekend(self):
        before_open = datetime(2024, 7, 3, 9, 0, tzinfo=engine.ET)
        entry, exit_ = engine.trading_schedule(1, before_open)
        self.assertEqual(entry, datetime(2024, 7, 3, 21, 30))
        self.assertEqual(exit_, datetime(2024, 7, 5, 21, 30))

        after_open = datetime(2024, 7, 3, 10, 0, tzinfo=engine.ET)
        entry, exit_ = engine.trading_schedule(1, after_open)
        self.assertEqual(entry, datetime(2024, 7, 5, 21, 30))
        self.assertEqual(exit_, datetime(2024, 7, 8, 21, 30))

        winter_open = datetime(2024, 1, 3, 9, 0, tzinfo=engine.ET)
        winter_entry, _ = engine.trading_schedule(1, winter_open)
        self.assertEqual(winter_entry, datetime(2024, 1, 3, 22, 30))

    def test_juneteenth_calendar_effective_year(self):
        self.assertTrue(engine.is_nyse_trading_day(datetime(2021, 6, 18).date()))
        self.assertFalse(engine.is_nyse_trading_day(datetime(2024, 6, 19).date()))


class CacheTests(unittest.TestCase):
    def test_project_env_file_loads_without_overriding_shell_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = os.path.join(directory, ".env")
            with open(env_path, "w", encoding="utf-8") as env_file:
                env_file.write("EXA_API_KEY=from-dotenv\n")
            with patch.dict(os.environ, {}, clear=True):
                engine.load_project_environment(env_path)
                self.assertEqual(os.environ["EXA_API_KEY"], "from-dotenv")
                os.environ["EXA_API_KEY"] = "from-shell"
                engine.load_project_environment(env_path)
                self.assertEqual(os.environ["EXA_API_KEY"], "from-shell")

    def test_cached_exa_call_reuses_result(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = os.path.join(directory, ".alpha_cache.json")
            calls = []
            with patch.object(engine, "CACHE_FILE", cache_path):
                first = engine._cached_exa_call("same-request", lambda: calls.append(1) or "result")
                second = engine._cached_exa_call("same-request", lambda: calls.append(2) or "new")
            self.assertEqual((first, second), ("result", "result"))
            self.assertEqual(calls, [1])
            self.assertTrue(os.path.exists(cache_path))

    def test_exa_calls_use_modern_schema_and_no_full_text(self):
        response = {
            "direction": "BULLISH",
            "is_negated_or_rumor": False,
            "catalyst_type": "Guidance_Hike",
            "confidence": 0.9,
            "rationale": "Management raised its forward guidance.",
        }
        exa = Mock()
        exa.get_contents.return_value = SimpleNamespace(
            results=[SimpleNamespace(highlights=["Raises full-year outlook."])]
        )
        exa.search.return_value = SimpleNamespace(
            output=SimpleNamespace(content=json.dumps(response))
        )
        item = {"title": "Company raises guidance", "url": "https://example.com/news"}
        config = {"direction": "BULLISH", "event_key": "Guidance_Hike"}

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(engine, "CACHE_FILE", os.path.join(directory, ".alpha_cache.json")):
                first = engine.semantic_extract(item, config, exa=exa)
                second = engine.semantic_extract(item, config, exa=exa)

        self.assertEqual(first, second)
        self.assertEqual(
            set(first),
            {"direction", "is_negated_or_rumor", "catalyst_type", "confidence", "rationale"},
        )
        exa.get_contents.assert_called_once_with(
            ["https://example.com/news"],
            highlights={"max_characters": 1200},
            text=False,
        )
        exa.search.assert_called_once()
        self.assertEqual(exa.search.call_args.kwargs["type"], "auto")
        self.assertEqual(exa.search.call_args.kwargs["output_schema"], engine.SEMANTIC_OUTPUT_SCHEMA)
        self.assertLessEqual(len(engine.SEMANTIC_OUTPUT_SCHEMA["properties"]), 10)
        prompt = exa.search.call_args.args[0]
        self.assertIn("Independently assess", prompt)
        self.assertNotIn("Guidance_Hike", prompt)

    def test_rationale_abbreviations_do_not_trigger_fallback(self):
        response = {
            "direction": "NEUTRAL",
            "is_negated_or_rumor": False,
            "catalyst_type": "SEC_FILING",
            "confidence": 0.8,
            "rationale": "The U.S. filing is procedural. It does not disclose a new catalyst.",
        }
        parsed = engine._parse_semantic_output(response)
        self.assertEqual(parsed["rationale"], "The U.S. filing is procedural.")


class ScanPersistenceTests(unittest.TestCase):
    def test_scanned_semantic_source_survives_ledger_roundtrip(self):
        config = {
            "direction": "BULLISH",
            "event_key": "Guidance_Hike",
            "story": "Raised guidance",
            "horizon_days": 5,
        }
        semantic = {
            "direction": "BEARISH",
            "is_negated_or_rumor": False,
            "catalyst_type": "Guidance_Cut",
            "confidence": 0.8,
            "rationale": "The company lowered its outlook.",
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(engine, "LEDGER_FILE", os.path.join(directory, "signals.csv")),
                patch.object(engine, "GLOBAL_TICKER_MAP", {"ACME": "ACME"}),
                patch.object(
                    engine, "fetch_sec_filings",
                    return_value=[{
                        "title": "ACME raises guidance",
                        "source": "SEC",
                        "url": "https://example.com/filing",
                    }],
                ),
                patch.object(engine.feedparser, "parse", return_value=SimpleNamespace(entries=[])),
                patch.object(engine, "fetch_finnhub_news", return_value=[]),
                patch.object(engine, "fetch_alphavantage_news", return_value=[]),
                patch.object(engine, "fetch_polygon_news", return_value=[]),
                patch.object(engine, "check_all_playbooks", return_value=("Guidance_Hike", config, "KEYWORD")),
                patch.object(engine, "find_ticker_in_headline", return_value="ACME"),
                patch.object(engine, "semantic_extract", return_value=(semantic, "EXA")),
                patch.object(
                    engine, "get_bulk_market_data",
                    return_value={"ACME": {"price": 10.0, "volume": 300_000}},
                ),
            ):
                scanned = engine.scan_news(no_exa=True)
                engine.manage_ledger(scanned)
                persisted = engine.load_ledger()

        self.assertEqual(persisted.loc[0, "rule_direction"], "BULLISH")
        self.assertEqual(persisted.loc[0, "semantic_direction"], "BEARISH")
        self.assertEqual(persisted.loc[0, "semantic_source"], "EXA")
        self.assertFalse(persisted.loc[0, "agreement"])


class EvaluationTests(unittest.TestCase):
    def test_market_neutral_metrics_use_raw_return_and_correct_short_hedge(self):
        rows = []
        for ticker, direction, entry, exit_, spy_entry, spy_exit in (
            ("GLOB", "BEARISH", 50.54, 45.47, 666.39, 669.27),
            ("LONG", "BULLISH", 100.0, 110.0, 100.0, 105.0),
            ("FLAT", "NEUTRAL", 100.0, 101.0, 100.0, 100.0),
            ("NEWS", "NEUTRAL", 100.0, 105.0, 100.0, 100.0),
        ):
            row = {column: None for column in engine.LEDGER_COLUMNS}
            row.update({
                "Ticker": ticker,
                "status": "CLOSED",
                "rule_direction": direction,
                "semantic_direction": None,
                "semantic_source": None,
                "entry_price": entry,
                "exit_price": exit_,
                "spy_entry_price": spy_entry,
                "spy_exit_price": spy_exit,
            })
            rows.append(row)

        result = engine.recalculate_performance_metrics(pd.DataFrame(rows)).set_index("Ticker")
        glob = result.loc["GLOB"]
        self.assertAlmostEqual(glob["raw_stock_return_pct"], (45.47 / 50.54 - 1) * 100)
        self.assertAlmostEqual(glob["signal_return_pct"], (1 - 45.47 / 50.54) * 100)
        self.assertAlmostEqual(
            glob["market_neutral_alpha_pct"],
            (669.27 / 666.39 - 1) * 100 - (45.47 / 50.54 - 1) * 100,
        )
        self.assertTrue(glob["direction_matched"])
        self.assertTrue(glob["beat_spy_relative"])
        self.assertEqual(result.loc["FLAT", "neutral_move_classification"], "NOISE_CONFIRMED")
        self.assertTrue(result.loc["FLAT", "direction_matched"])
        self.assertEqual(result.loc["NEWS", "neutral_move_classification"], "MATERIAL_SURPRISE")
        self.assertFalse(result.loc["NEWS", "direction_matched"])
        self.assertTrue(pd.isna(result.loc["FLAT", "beat_spy_relative"]))

    def test_closed_rows_recalculate_without_downloading_prices_again(self):
        row = {column: None for column in engine.LEDGER_COLUMNS}
        row.update({
            "Ticker": "GLOB",
            "Direction": "BEARISH",
            "rule_direction": "BEARISH",
            "status": "CLOSED",
            "entry_price": 50.54,
            "exit_price": 45.47,
            "spy_entry_price": 666.39,
            "spy_exit_price": 669.27,
        })
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(engine, "LEDGER_FILE", os.path.join(directory, "signals.csv")),
                patch.object(engine, "REPORT_FILE", os.path.join(directory, "report.md")),
                patch.object(engine.yf, "download") as download,
            ):
                engine.save_ledger(pd.DataFrame([row]))
                evaluated = engine.evaluate_ledger()

        raw_return = (45.47 / 50.54 - 1) * 100
        spy_return = (669.27 / 666.39 - 1) * 100
        self.assertAlmostEqual(
            evaluated.loc[0, "market_neutral_alpha_pct"], spy_return - raw_return
        )
        self.assertEqual(evaluated.loc[0, "status"], "CLOSED")
        download.assert_not_called()

    def test_missing_entry_duplicate_key_uses_headline_identity(self):
        row = {column: None for column in engine.LEDGER_COLUMNS}
        row.update({
            "Ticker": "HPQ",
            "Event_Type": "Analyst_Upgrade",
            "Source": "gnews_targeted",
            "Headline": "HP shares rise",
            "Exit_Date": "2026-12-04",
        })
        ledger = pd.DataFrame([row, dict(row)])
        self.assertEqual(len(engine.deduplicate_signals(ledger)), 1)

    def test_legacy_semantic_fields_are_cleared_but_scan_results_are_kept(self):
        legacy = {column: None for column in engine.LEDGER_COLUMNS}
        legacy.update({
            "Entry_Date": "2025-12-01 22:30:00",
            "Ticker": "OLD",
            "Direction": "BULLISH",
            "rule_direction": "BULLISH",
            "semantic_direction": "BULLISH",
            "agreement": True,
            "semantic_excess_alpha_pct": 2.5,
            "status": "CLOSED",
        })
        scanned = dict(legacy)
        scanned.update({
            "Ticker": "NEW",
            "semantic_direction": "BEARISH",
            "semantic_source": "EXA",
            "agreement": False,
        })
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "signals.csv")
            pd.DataFrame([legacy, scanned]).to_csv(path, index=False)
            with patch.object(engine, "LEDGER_FILE", path):
                ledger = engine.load_ledger()

        old_row = ledger.loc[ledger["Ticker"] == "OLD"].iloc[0]
        new_row = ledger.loc[ledger["Ticker"] == "NEW"].iloc[0]
        self.assertTrue(pd.isna(old_row["semantic_direction"]))
        self.assertTrue(pd.isna(old_row["agreement"]))
        self.assertTrue(pd.isna(old_row["semantic_excess_alpha_pct"]))
        self.assertEqual(new_row["semantic_direction"], "BEARISH")
        self.assertEqual(new_row["semantic_source"], "EXA")

    def test_due_signal_closes_and_persists_spy_benchmarked_returns(self):
        entry = datetime(2024, 7, 3, 21, 30)
        exit_ = datetime(2024, 7, 5, 21, 30)
        row = {column: None for column in engine.LEDGER_COLUMNS}
        row.update({
            "Entry_Date": entry,
            "Exit_Date": exit_,
            "Ticker": "ACME",
            "Direction": "BULLISH",
            "rule_direction": "BULLISH",
            "semantic_direction": "BEARISH",
            "semantic_source": "EXA",
            "status": "OPEN",
            "Event_Type": "Guidance_Hike",
        })
        dates = pd.to_datetime(["2024-07-03", "2024-07-05"])
        columns = pd.MultiIndex.from_product([["ACME", "SPY"], ["Open"]])
        bars = pd.DataFrame(
            [[10.0, 100.0], [12.0, 110.0]],
            index=dates,
            columns=columns,
        )

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(engine, "LEDGER_FILE", os.path.join(directory, "signals.csv")),
                patch.object(engine, "REPORT_FILE", os.path.join(directory, "report.md")),
                patch.object(engine.yf, "download", return_value=bars),
            ):
                engine.save_ledger(pd.DataFrame([row]))
                closed = engine.evaluate_ledger(
                    now=datetime(2024, 7, 5, 21, 31, tzinfo=engine.SGT)
                )
                persisted = pd.read_csv(engine.LEDGER_FILE)
                with open(engine.REPORT_FILE, encoding="utf-8") as report:
                    report_text = report.read()

        self.assertEqual(closed.loc[0, "status"], "CLOSED")
        self.assertAlmostEqual(closed.loc[0, "signal_return_pct"], 20.0)
        self.assertAlmostEqual(closed.loc[0, "spy_return_pct"], 10.0)
        self.assertAlmostEqual(closed.loc[0, "excess_alpha_pct"], 10.0)
        self.assertAlmostEqual(closed.loc[0, "semantic_excess_alpha_pct"], -10.0)
        self.assertEqual(persisted.loc[0, "status"], "CLOSED")
        self.assertIn("Exa-semantic-direction performance", report_text)

    def test_old_non_trading_row_is_reopened_and_uses_next_sessions(self):
        row = {column: None for column in engine.LEDGER_COLUMNS}
        row.update({
            "Entry_Date": datetime(2025, 12, 6, 22, 30),
            "Exit_Date": datetime(2025, 12, 11, 22, 30),
            "Ticker": "ADBE",
            "rule_direction": "BULLISH",
            "semantic_direction": "BULLISH",
            "status": "UNVERIFIABLE",
            "evaluation_note": "entry 2025-12-06 is not an NYSE trading day.",
        })
        dates = pd.to_datetime(["2025-12-08", "2025-12-11"])
        columns = pd.MultiIndex.from_product([["ADBE", "SPY"], ["Open"]])
        bars = pd.DataFrame(
            [[100.0, 500.0], [110.0, 510.0]],
            index=dates,
            columns=columns,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(engine, "LEDGER_FILE", os.path.join(directory, "signals.csv")),
                patch.object(engine, "REPORT_FILE", os.path.join(directory, "report.md")),
                patch.object(engine.yf, "download", return_value=bars) as download,
            ):
                engine.save_ledger(pd.DataFrame([row]))
                first = engine.evaluate_ledger(
                    now=datetime(2025, 12, 12, 12, 0, tzinfo=engine.SGT)
                )
                second = engine.evaluate_ledger(
                    now=datetime(2025, 12, 13, 12, 0, tzinfo=engine.SGT)
                )
                with open(engine.REPORT_FILE, encoding="utf-8") as report_file:
                    report = report_file.read()

        self.assertEqual(first.loc[0, "status"], "CLOSED")
        self.assertEqual(first.loc[0, "actual_entry_date"], pd.Timestamp("2025-12-08 22:30:00"))
        self.assertEqual(first.loc[0, "actual_exit_date"], pd.Timestamp("2025-12-11 22:30:00"))
        self.assertIn("next session 2025-12-08", first.loc[0, "evaluation_note"])
        self.assertEqual(second.loc[0, "status"], "CLOSED")
        self.assertEqual(download.call_count, 1)
        self.assertIn("Trading date adjustments", report)
        self.assertIn("actual 2025-12-08 22:30:00 to 2025-12-11 22:30:00", report)

    def test_saved_actual_session_keeps_weekend_adjustment_reason(self):
        row = {column: None for column in engine.LEDGER_COLUMNS}
        row.update({
            "Entry_Date": datetime(2025, 12, 6, 22, 30),
            "Exit_Date": datetime(2025, 12, 11, 22, 30),
            "actual_entry_date": datetime(2025, 12, 8, 22, 30),
            "actual_exit_date": datetime(2025, 12, 11, 22, 30),
            "Ticker": "ACME",
            "rule_direction": "BULLISH",
            "status": "OPEN",
        })
        dates = pd.to_datetime(["2025-12-08", "2025-12-11"])
        columns = pd.MultiIndex.from_product([["ACME", "SPY"], ["Open"]])
        bars = pd.DataFrame(
            [[100.0, 500.0], [110.0, 510.0]],
            index=dates,
            columns=columns,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(engine, "LEDGER_FILE", os.path.join(directory, "signals.csv")),
                patch.object(engine, "REPORT_FILE", os.path.join(directory, "report.md")),
                patch.object(engine.yf, "download", return_value=bars),
            ):
                engine.save_ledger(pd.DataFrame([row]))
                evaluated = engine.evaluate_ledger(
                    now=datetime(2025, 12, 12, 12, 0, tzinfo=engine.SGT)
                )

        self.assertEqual(evaluated.loc[0, "status"], "CLOSED")
        self.assertIn(
            "Scheduled entry 2025-12-06 (Saturday) aligned to actual NYSE session 2025-12-08",
            evaluated.loc[0, "evaluation_note"],
        )

    def test_missing_exact_bar_is_recorded_and_not_retried(self):
        row = {column: None for column in engine.LEDGER_COLUMNS}
        row.update({
            "Entry_Date": datetime(2024, 7, 3, 21, 30),
            "Exit_Date": datetime(2024, 7, 5, 21, 30),
            "Ticker": "ACME",
            "rule_direction": "BULLISH",
            "semantic_direction": "BULLISH",
            "status": "OPEN",
        })
        dates = pd.to_datetime(["2024-07-03", "2024-07-05"])
        columns = pd.MultiIndex.from_product([["ACME", "SPY"], ["Open"]])
        bars = pd.DataFrame(
            [[10.0, 100.0], [None, 110.0]],
            index=dates,
            columns=columns,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(engine, "LEDGER_FILE", os.path.join(directory, "signals.csv")),
                patch.object(engine, "REPORT_FILE", os.path.join(directory, "report.md")),
                patch.object(engine.yf, "download", return_value=bars) as download,
            ):
                engine.save_ledger(pd.DataFrame([row]))
                first = engine.evaluate_ledger(
                    now=datetime(2024, 7, 6, 12, 0, tzinfo=engine.SGT)
                )
                second = engine.evaluate_ledger(
                    now=datetime(2024, 7, 7, 12, 0, tzinfo=engine.SGT)
                )

        self.assertEqual(first.loc[0, "status"], "UNVERIFIABLE")
        self.assertIn("ACME 2024-07-05", first.loc[0, "evaluation_note"])
        self.assertIn("Missing exact-date daily open bar(s)", first.loc[0, "evaluation_note"])
        self.assertEqual(second.loc[0, "status"], "UNVERIFIABLE")
        self.assertEqual(download.call_count, 1)


if __name__ == "__main__":
    unittest.main()

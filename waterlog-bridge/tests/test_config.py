from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

from waterlog_bridge.config import ConfigError, load_config
from waterlog_bridge.logging_utils import RedactingFormatter, SecretRedactionFilter


STREAM_ID = "11111111-1111-4111-8111-111111111111"
SECRET = "wlb_this-is-a-long-test-secret"


def valid_options() -> dict[str, object]:
    return {
        "waterlog_url": "https://waterlog.fish/",
        "waterlog_credential": SECRET,
        "streams": [
            {
                "stream_id": STREAM_ID,
                "entity_id": "sensor.reefato_temperature",
                "unit_override": "°F",
            }
        ],
    }


class ConfigTests(unittest.TestCase):
    def load(self, options: dict[str, object]):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "options.json")
            path.write_text(json.dumps(options), encoding="utf-8")
            return load_config(path)

    def test_loads_and_normalizes_valid_options_without_secret_repr(self) -> None:
        config = self.load(valid_options())
        self.assertEqual(config.waterlog_url, "https://waterlog.fish")
        self.assertEqual(config.sample_interval_seconds, 300)
        self.assertEqual(config.streams[0].stream_id, STREAM_ID)
        self.assertNotIn(SECRET, repr(config))

    def test_https_is_required_by_default(self) -> None:
        options = valid_options()
        options["waterlog_url"] = "http://waterlog.example"
        with self.assertRaisesRegex(ConfigError, "must use HTTPS"):
            self.load(options)

    def test_duplicate_entities_are_rejected(self) -> None:
        options = valid_options()
        options["streams"] = [
            {"stream_id": STREAM_ID, "entity_id": "sensor.temperature"},
            {
                "stream_id": "22222222-2222-4222-8222-222222222222",
                "entity_id": "sensor.temperature",
            },
        ]
        with self.assertRaisesRegex(ConfigError, "entity_id values must be unique"):
            self.load(options)

    def test_empty_stream_list_is_rejected(self) -> None:
        options = valid_options()
        options["streams"] = []
        with self.assertRaisesRegex(ConfigError, "at least one stream"):
            self.load(options)

    def test_removed_hydros_options_do_not_change_home_assistant_config(self) -> None:
        options = valid_options()
        options.update(
            {
                "hydros_provider_key": None,
                "hydros_devices": [],
                "hydros_streams": [],
            }
        )
        config = self.load(options)
        self.assertEqual(len(config.streams), 1)
        self.assertEqual(config.streams[0].entity_id, "sensor.reefato_temperature")

    def test_removed_hydros_options_cannot_replace_home_assistant_streams(self) -> None:
        options = valid_options()
        options.update(
            {
                "streams": [],
                "hydros_provider_key": "provider-key-not-used-by-0.2.1",
                "hydros_devices": [
                    {"name": "lagoon-launch", "device_key": "device-key"}
                ],
                "hydros_streams": [
                    {
                        "stream_id": STREAM_ID,
                        "device": "lagoon-launch",
                        "input": "pH",
                        "unit": "pH",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ConfigError, "at least one stream"):
            self.load(options)

    def test_sample_interval_is_fixed_at_five_minutes(self) -> None:
        options = valid_options()
        options["sample_interval_seconds"] = 600
        with self.assertRaisesRegex(ConfigError, "between 300 and 300"):
            self.load(options)

    def test_redaction_filter_removes_bearer_and_known_secret(self) -> None:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "Authorization: Bearer %s credential=%s",
            (SECRET, SECRET),
            None,
        )
        SecretRedactionFilter((SECRET,)).filter(record)
        rendered = record.getMessage()
        self.assertNotIn(SECRET, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_redacting_formatter_covers_exception_text(self) -> None:
        try:
            raise RuntimeError(SECRET)
        except RuntimeError:
            exception = sys.exc_info()
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", (), exception
        )
        rendered = RedactingFormatter("%(message)s", (SECRET,)).format(record)
        self.assertNotIn(SECRET, rendered)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()

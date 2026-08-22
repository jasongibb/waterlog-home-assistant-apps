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

    def test_sample_interval_is_fixed_at_five_minutes(self) -> None:
        options = valid_options()
        options["sample_interval_seconds"] = 600
        with self.assertRaisesRegex(ConfigError, "between 300 and 300"):
            self.load(options)

    # -- HYDROS options -------------------------------------------------------

    def hydros_options(self) -> dict[str, object]:
        options = valid_options()
        options["hydros_provider_key"] = "provider-secret-key-value"
        options["hydros_devices"] = [{"name": "lagoon-launch", "device_key": "a" * 20}]
        options["hydros_streams"] = [
            {
                "stream_id": "33333333-3333-4333-8333-333333333333",
                "device": "lagoon-launch",
                "input": "pH",
                "unit": "pH",
            }
        ]
        return options

    def test_loads_valid_hydros_options(self) -> None:
        config = self.load(self.hydros_options())
        self.assertEqual(config.hydros_provider_key, "provider-secret-key-value")
        self.assertEqual(len(config.hydros_devices), 1)
        self.assertEqual(config.hydros_devices[0].name, "lagoon-launch")
        self.assertEqual(len(config.hydros_streams), 1)
        self.assertEqual(config.hydros_streams[0].input_name, "pH")
        self.assertEqual(config.hydros_streams[0].unit, "pH")
        self.assertNotIn("provider-secret-key-value", repr(config))
        self.assertNotIn("a" * 20, repr(config))

    def test_ha_streams_may_be_empty_when_hydros_streams_exist(self) -> None:
        options = self.hydros_options()
        options["streams"] = []
        config = self.load(options)
        self.assertEqual(config.streams, ())
        self.assertEqual(len(config.hydros_streams), 1)

    def test_both_stream_lists_empty_is_still_rejected(self) -> None:
        options = valid_options()
        options["streams"] = []
        with self.assertRaisesRegex(ConfigError, "at least one stream"):
            self.load(options)

    def test_hydros_provider_key_required_when_devices_configured(self) -> None:
        options = self.hydros_options()
        del options["hydros_provider_key"]
        with self.assertRaisesRegex(ConfigError, "hydros_provider_key is required"):
            self.load(options)

    def test_hydros_provider_key_blank_is_rejected_when_devices_configured(self) -> None:
        options = self.hydros_options()
        options["hydros_provider_key"] = "   "
        with self.assertRaisesRegex(ConfigError, "hydros_provider_key is required"):
            self.load(options)

    def test_hydros_provider_key_control_characters_rejected(self) -> None:
        options = self.hydros_options()
        options["hydros_provider_key"] = "provider\nsecret-key-value"
        with self.assertRaisesRegex(ConfigError, "invalid whitespace or control characters"):
            self.load(options)

    def test_hydros_device_name_regex_is_enforced(self) -> None:
        options = self.hydros_options()
        options["hydros_devices"] = [{"name": "Lagoon Launch", "device_key": "a" * 20}]
        with self.assertRaisesRegex(ConfigError, "hydros_devices\\[0\\].name"):
            self.load(options)

    def test_hydros_device_names_must_be_unique(self) -> None:
        options = self.hydros_options()
        options["hydros_devices"] = [
            {"name": "lagoon-launch", "device_key": "a" * 20},
            {"name": "lagoon-launch", "device_key": "b" * 20},
        ]
        with self.assertRaisesRegex(ConfigError, "hydros_devices\\[\\].name values must be unique"):
            self.load(options)

    def test_hydros_device_key_minimum_length_is_enforced(self) -> None:
        options = self.hydros_options()
        options["hydros_devices"] = [{"name": "lagoon-launch", "device_key": "short"}]
        with self.assertRaisesRegex(ConfigError, "device_key must be at least 16 characters"):
            self.load(options)

    def test_hydros_device_key_rejects_control_characters(self) -> None:
        options = self.hydros_options()
        options["hydros_devices"] = [
            {"name": "lagoon-launch", "device_key": "a" * 10 + "\t" + "a" * 10}
        ]
        with self.assertRaisesRegex(ConfigError, "invalid whitespace or control characters"):
            self.load(options)

    def test_hydros_devices_limit_of_ten_is_enforced(self) -> None:
        options = self.hydros_options()
        options["hydros_devices"] = [
            {"name": f"device-{index}", "device_key": "a" * 20} for index in range(11)
        ]
        options["hydros_streams"] = [
            {
                "stream_id": "33333333-3333-4333-8333-333333333333",
                "device": "device-0",
                "input": "pH",
                "unit": "pH",
            }
        ]
        with self.assertRaisesRegex(ConfigError, "no more than 10 hydros_devices"):
            self.load(options)

    def test_hydros_stream_id_must_be_unique_across_ha_and_hydros_lists(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"][0]["stream_id"] = STREAM_ID  # collides with the HA stream
        with self.assertRaisesRegex(ConfigError, "stream_id values must be unique"):
            self.load(options)

    def test_hydros_stream_device_must_reference_a_defined_device(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"][0]["device"] = "not-configured"
        with self.assertRaisesRegex(ConfigError, "must name a configured hydros device"):
            self.load(options)

    def test_hydros_stream_input_length_is_enforced(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"][0]["input"] = ""
        with self.assertRaisesRegex(ConfigError, "input must be 1-100 characters"):
            self.load(options)

    def test_hydros_stream_input_rejects_control_characters(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"][0]["input"] = "pH\x01"
        with self.assertRaisesRegex(ConfigError, "input must be 1-100 characters"):
            self.load(options)

    def test_hydros_stream_value_field_regex_is_enforced(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"][0]["value_field"] = "9probeValue"
        with self.assertRaisesRegex(ConfigError, "value_field must match"):
            self.load(options)

    def test_hydros_stream_value_field_override_loads(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"][0]["value_field"] = "probeRawValue"
        config = self.load(options)
        self.assertEqual(config.hydros_streams[0].value_field, "probeRawValue")

    def test_hydros_stream_unit_is_required(self) -> None:
        options = self.hydros_options()
        del options["hydros_streams"][0]["unit"]
        with self.assertRaisesRegex(ConfigError, "unit is required"):
            self.load(options)

    def test_hydros_stream_unit_rejects_control_characters(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"][0]["unit"] = "p\x00H"
        with self.assertRaisesRegex(ConfigError, "unit is required"):
            self.load(options)

    def test_hydros_streams_limit_of_100_is_enforced(self) -> None:
        options = self.hydros_options()
        options["hydros_streams"] = [
            {
                "stream_id": f"44444444-4444-4444-8444-4444444444{index:02d}",
                "device": "lagoon-launch",
                "input": f"Sensor {index}",
                "unit": "pH",
            }
            for index in range(101)
        ]
        with self.assertRaisesRegex(ConfigError, "no more than 100 hydros_streams"):
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

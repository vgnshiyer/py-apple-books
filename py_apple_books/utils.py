import configparser
import pathlib
from datetime import datetime

# Seconds between Unix epoch (1970-01-01) and Apple/Core Data epoch (2001-01-01)
APPLE_EPOCH_OFFSET = 978307200


def get_mappings(model_name: str) -> dict:
    mappings_path = pathlib.Path(__file__).parent / "mappings.ini"
    config = configparser.ConfigParser()
    config.read(mappings_path)
    return dict(config.items(model_name))


def apple_timestamp_to_datetime(raw):
    """Convert an Apple/Core Data timestamp (seconds since 2001-01-01) to a datetime."""
    if raw is None:
        return None
    return datetime.fromtimestamp(float(raw) + APPLE_EPOCH_OFFSET)

import configparser
import pathlib


def get_mappings(model_name: str) -> dict:
    mappings_path = pathlib.Path(__file__).parent / "mappings.ini"
    config = configparser.ConfigParser()
    config.read(mappings_path)
    return dict(config.items(model_name))

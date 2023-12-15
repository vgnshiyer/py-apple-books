import configparser
import pathlib

def get_mappings(class_name: str) -> dict:
    mappings_path = pathlib.Path(__file__).parent / "mappings.ini"
    config = configparser.ConfigParser()
    config.read(mappings_path)
    return dict(config.items(class_name))
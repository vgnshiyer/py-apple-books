from dataclasses import dataclass
from datetime import datetime

@dataclass
class Annotation:
    # Identifiers
    id: str
    asset_id: str

    # Status
    is_deleted: bool

    # Dates
    creation_date: datetime
    modification_date: datetime

    # Annotation details
    representative_text: str
    selected_text: str
    note: str

    # Location
    chapter: str
    location: str

    def __post_init__(self):
        self.creation_date = datetime.fromtimestamp(float(self.creation_date) / 1000)
        self.modification_date = datetime.fromtimestamp(float(self.modification_date) / 1000)
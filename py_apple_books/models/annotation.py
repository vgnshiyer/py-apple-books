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
        """
        Converts the creation_date and modification_date from timestamp to datetime.
        """
        self.creation_date = datetime.fromtimestamp(float(self.creation_date) / 1000) if self.creation_date else None
        self.modification_date = datetime.fromtimestamp(float(self.modification_date) / 1000) if self.modification_date else None

    def __hash__(self):
        """
        Returns a hash value for the Annotation object.

        Returns:
            int: hash value for the Annotation object
        """
        return hash(self.id)
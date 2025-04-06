class ManagerBase:
    """Base class for all managers with common functionality."""

    def __init__(self):
        self._style_mappings = {
            1: 'green',
            2: 'blue',
            3: 'yellow',
            4: 'pink',
            5: 'purple',
        }
        self._reverse_style_mappings = {v: k for k, v in self._style_mappings.items()}

from py_apple_books.data import annotation_db
from py_apple_books.exceptions import AnnotationNotFoundError
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.highlight import Highlight
from py_apple_books.models.underline import Underline
from py_apple_books.managers.base import ManagerBase
from py_apple_books.utils import get_mappings
from typing import List, Optional


class AnnotationManager(ManagerBase):
    """Manages annotation-related operations."""

    def __init__(self):
        super().__init__()
        self._annotation_mappings = get_mappings('Annotation')

    def _create_annotation_object(self, annotation: dict) -> Annotation:
        is_underline = annotation.pop('is_underline')
        style = annotation.pop('style')
        if is_underline:
            return Underline(**annotation)
        elif style in self._style_mappings:
            annotation['color'] = self._style_mappings[style]
            return Highlight(**annotation)
        else:
            return Annotation(**annotation)

    def list_annotations(self, type: str = 'all') -> List[Annotation]:
        """
        Fetches a list of Annotation objects.

        Returns:
            list[Annotation]: list of Annotation objects
        """
        list_of_annotations = []
        if type == 'all':
            raw_annotation_data = annotation_db.find_all()
        elif type == 'highlights':
            raw_annotation_data = annotation_db.find_highlights()
        elif type == 'underlines':
            raw_annotation_data = annotation_db.find_underlines()
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def list_all_notes(self) -> list[Annotation]:
        """
        Fetches a list of Annotation objects with notes.

        Returns:
            list[Annotation]: list of Annotation objects
        """
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_notes()
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def get_annotation_by_id(self, annotation_id: str) -> Optional[Annotation]:
        """
        Fetches an Annotation object by id.

        Args:
            annotation_id (str): annotation id

        Returns:
            Annotation: Annotation object
        """
        raw_annotation_data = annotation_db.find_by_id(annotation_id)
        if not raw_annotation_data:
            raise AnnotationNotFoundError(annotation_id=annotation_id)
        raw_annotation_data = raw_annotation_data[0]
        annotation = dict(zip(self._annotation_mappings.keys(), raw_annotation_data))
        return self._create_annotation_object(annotation)

    def get_annotations_by_text(self, text: str) -> list[Annotation]:
        """
        Fetches Annotations by text.

        Args:
            text (str): text to search for

        Returns:
            list[Annotation]: list of Annotation objects
        """
        raw_annotation_data = annotation_db.find_by_representative_text(text)
        raw_annotation_data += annotation_db.find_by_selected_text(text)
        raw_annotation_data += annotation_db.find_by_note_text(text)
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        list_of_annotations = []
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def get_annotation_by_color(self, color: str) -> list[Annotation]:
        """
        Fetches a list of Annotation objects by color.

        Args:
            color (str): color

        Returns:
            list[Annotation]: list of Annotation objects
        """
        if color not in self._reverse_style_mappings:
            raise Exception(f"Invalid color {color}")

        list_of_annotations = []
        raw_annotation_data = annotation_db.find_by_style(self._reverse_style_mappings[color])
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

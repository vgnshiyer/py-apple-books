from py_apple_books import PyAppleBooks

api = PyAppleBooks()

# Find all yellow highlights
notes = api.get_annotation_by_color('green')
for note in notes:
    print('-'*50)
    print(f'Selected text: {note.selected_text}')
    print(f'Color: {note.color}')
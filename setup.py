import re

from setuptools import setup, find_packages

with open('README.md') as f:
    long_description = f.read()

# Single source for the version: py_apple_books/__init__.py. Parsed
# rather than imported so building doesn't need the runtime deps.
with open('py_apple_books/__init__.py') as f:
    version = re.search(
        r"^__version__ = ['\"]([^'\"]+)['\"]", f.read(), re.M
    ).group(1)

setup(
    name='py_apple_books',
    version=version,
    description='Python library for Apple Books',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Vignesh Iyer',
    author_email='vgnshiyer@gmail.com',
    packages=find_packages(exclude=['tests', 'tests.*']),
    # 3.10+: PEP 604 `X | None` annotations are evaluated at import.
    python_requires='>=3.10',
    install_requires=[
        # EPUB parsing: handles EPUB2 (NCX) and EPUB3 (nav doc) ToCs.
        'ebooklib>=0.20',
        # XHTML → plain text for chapter content extraction.
        'beautifulsoup4>=4.12',
    ],
    extras_require={
        'dev': ['pytest>=7.0'],
    },
    license='MIT',
    url='https://github.com/vgnshiyer/py-apple-books',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'License :: OSI Approved :: MIT License',
        'Operating System :: MacOS',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Typing :: Typed',
    ],
    package_data={
        'py_apple_books': ['py.typed', 'models/*.ini'],
    },
)

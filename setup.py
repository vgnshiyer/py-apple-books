from setuptools import setup, find_packages

with open('README.md') as f:
    long_description = f.read()

setup(
    name='py_apple_books',
    version='1.6.0',
    description='Python library for Apple Books',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Vignesh Iyer',
    author_email='vgnshiyer@gmail.com',
    packages=find_packages(),
    install_requires=[
        # EPUB parsing: handles EPUB2 (NCX) and EPUB3 (nav doc) ToCs.
        'ebooklib>=0.20',
        # XHTML → plain text for chapter content extraction.
        'beautifulsoup4>=4.12',
    ],
    license='MIT',
    url='https://github.com/vgnshiyer/py-apple-books',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Build Tools',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
    ],
    package_data={
        'py_apple_books': ['*.ini', 'models/*.ini'],
    },
)

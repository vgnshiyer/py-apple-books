from setuptools import setup, find_packages

with open('README.md') as f:
    long_description = f.read()

setup(
    # name of the package -> must be the same as the name of the folder
    name='py_apple_books',
    version='0.2.0',
    description='Python library for Apple Books',
    long_description=long_description,
    long_description_content_type='text/markdown',
    # author details
    author='Vignesh Iyer',
    author_email='vgnshiyer@asu.edu',
    packages=find_packages(),
    license='MIT',
    # projects official homepage
    url='https://github.com/vgnshiyer/py-apple-books',
    classifiers=[
        # How mature is this project? Common values are
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        'Development Status :: 3 - Alpha',

        # who your project is intended for
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Build Tools',

        # license
        'License :: OSI Approved :: MIT License',

        # python versions supported
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
    ],

    package_data={
        'py_apple_books': ['*.ini'],
    },
)
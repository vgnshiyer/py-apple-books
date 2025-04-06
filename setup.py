from setuptools import setup, find_packages

with open('README.md') as f:
    long_description = f.read()

setup(
    # name of the package -> must be the same as the name of the folder
    name='py_apple_books',
    version='0.3',
    description='Python library for Apple Books',
    long_description=long_description,
    long_description_content_type='text/markdown',
    # author details
    author='Vignesh Iyer',
    author_email='vgnshiyer@gmail.com',
    packages=find_packages(),
    license='MIT',
    # projects official homepage
    url='https://github.com/vgnshiyer/py-apple-books',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Build Tools',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.0+',
    ],
    package_data={
        'py_apple_books': ['*.ini'],
    },
)

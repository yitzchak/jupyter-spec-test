#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import codecs
from setuptools import setup


def read(fname):
    file_path = os.path.join(os.path.dirname(__file__), fname)
    return codecs.open(file_path, encoding='utf-8').read()


setup(
    name='pytest-jupyter_kernel',
    version='0.1.0',
    author='Tarn W. Burton',
    author_email='twburton@gmail.com',
    maintainer='Tarn W. Burton',
    maintainer_email='twburton@gmail.com',
    license='MIT',
    url='https://github.com/yitzchak/pytest-jupyter_kernel',
    description='A Jupyter kernel fixture for pytest',
    long_description=read('README.md'),
    packages=['pytest_jupyter_kernel'],
    python_requires='>=3.5',
    install_requires=['pytest>=3.5.0','jsonschema>=3.2.0', 'jupyter-client>=6.1.0','pyzmq>=22.0.0'],
    include_package_data=True,
    classifiers=[
        'Development Status :: 4 - Beta',
        'Framework :: Pytest',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Testing',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: PyPy',
        'Operating System :: OS Independent',
        'License :: OSI Approved :: MIT License',
    ],
    entry_points={
        'pytest11': [
            'jupyter_kernel = pytest_jupyter_kernel',
        ],
    },
)

from setuptools import setup, find_packages

setup(
    name="hyperhttp",
    version="0.1.0",
    author="Latiful Mousom",
    author_email="latifulmousom@gmail.com",
    packages=find_packages(),
    package_data={"hyperhttp": ["py.typed"]},
)
from setuptools import setup, find_packages

# Use setuptools.setup as a minimal fallback for older tools
setup(
    name="hyperhttp",
    version="0.1.0",
    packages=find_packages(),
    package_data={"hyperhttp": ["py.typed"]},
    # The rest of the configuration is in pyproject.toml
)
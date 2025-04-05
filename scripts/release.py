#!/usr/bin/env python3
"""
Automate the release process for HyperHTTP.
Usage: python scripts/release.py [major|minor|patch]
"""

import argparse
import re
import subprocess
from datetime import datetime
from pathlib import Path

def get_current_version():
    """Get the current version from pyproject.toml."""
    pyproject = Path("pyproject.toml").read_text()
    version_match = re.search(r'version\s*=\s*"([^"]+)"', pyproject)
    return version_match.group(1) if version_match else None

def bump_version(current_version, bump_type):
    """Bump the version number."""
    major, minor, patch = map(int, current_version.split('.'))
    if bump_type == 'major':
        return f"{major + 1}.0.0"
    elif bump_type == 'minor':
        return f"{major}.{minor + 1}.0"
    else:  # patch
        return f"{major}.{minor}.{patch + 1}"

def update_pyproject(new_version):
    """Update version in pyproject.toml."""
    pyproject = Path("pyproject.toml")
    content = pyproject.read_text()
    updated = re.sub(
        r'(version\s*=\s*)"([^"]+)"',
        f'\\1"{new_version}"',
        content
    )
    pyproject.write_text(updated)

def update_changelog(new_version):
    """Update CHANGELOG.md with new version."""
    changelog = Path("docs/changelog.md")
    content = changelog.read_text()
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Move Unreleased changes to new version
    new_content = re.sub(
        r"## \[Unreleased\]\n\n(.*?)\n## ",
        f"## [Unreleased]\n\n### Added\n- N/A\n\n### Changed\n- N/A\n\n"
        f"### Deprecated\n- N/A\n\n### Removed\n- N/A\n\n### Fixed\n- N/A\n\n"
        f"### Security\n- N/A\n\n## [{new_version}] - {today}\n\n\\1\n\n## ",
        content,
        flags=re.DOTALL
    )
    
    # Update links at bottom
    new_content = re.sub(
        r"\[Unreleased\]: .*",
        f"[Unreleased]: https://github.com/lmousom/hyperhttp/compare/v{new_version}...HEAD\n"
        f"[{new_version}]: https://github.com/lmousom/hyperhttp/releases/tag/v{new_version}",
        new_content
    )
    
    changelog.write_text(new_content)

def main():
    parser = argparse.ArgumentParser(description="Release a new version of HyperHTTP")
    parser.add_argument('bump', choices=['major', 'minor', 'patch'],
                      help="Version part to bump")
    args = parser.parse_args()

    # Get current version
    current_version = get_current_version()
    if not current_version:
        print("Error: Could not find version in pyproject.toml")
        return 1

    # Calculate new version
    new_version = bump_version(current_version, args.bump)
    print(f"Bumping version from {current_version} to {new_version}")

    # Update files
    update_pyproject(new_version)
    update_changelog(new_version)

    # Git commands
    subprocess.run(["git", "add", "pyproject.toml", "docs/changelog.md"])
    subprocess.run(["git", "commit", "-m", f"Release version {new_version}"])
    subprocess.run(["git", "tag", "-a", f"v{new_version}", "-m", f"Release version {new_version}"])
    
    print(f"\nVersion {new_version} has been prepared for release.")
    print("\nNext steps:")
    print("1. Review the changes (git show HEAD)")
    print("2. Push the changes: git push origin main --tags")
    print("3. GitHub Actions will handle the rest!")

if __name__ == "__main__":
    main() 
# Contributing to HyperHTTP

Thank you for your interest in contributing to HyperHTTP! This document provides guidelines and instructions for contributing to the project.

## Development Setup

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/yourusername/hyperhttp.git
   cd hyperhttp
   ```

2. Create a virtual environment and activate it:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. Install development dependencies:
   ```bash
   pip install -e ".[dev,test,doc]"
   ```

## Code Style

We use the following tools to maintain code quality:

- **Black**: Code formatting
- **isort**: Import sorting
- **mypy**: Static type checking
- **flake8**: Code linting

Run all style checks:
```bash
# Format code
black .
isort .

# Check types
mypy hyperhttp

# Lint code
flake8
```

## Running Tests

We use pytest for testing:

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=hyperhttp

# Run specific test file
pytest tests/test_client.py

# Run specific test
pytest tests/test_client.py::test_get_request
```

## Documentation

We use MkDocs with Material theme for documentation:

1. Make changes to the docs in the `docs/` directory
2. Preview changes locally:
   ```bash
   mkdocs serve
   ```
3. Build documentation:
   ```bash
   mkdocs build
   ```

## Pull Request Process

1. Create a new branch for your feature:
   ```bash
   git checkout -b feature-name
   ```

2. Make your changes and commit them:
   ```bash
   git add .
   git commit -m "Description of changes"
   ```

3. Ensure all tests pass:
   ```bash
   pytest
   ```

4. Push to your fork:
   ```bash
   git push origin feature-name
   ```

5. Open a Pull Request with:
   - Clear description of changes
   - Any related issues
   - Test coverage for new features
   - Documentation updates if needed

## Commit Messages

Follow these guidelines for commit messages:

- Use present tense ("Add feature" not "Added feature")
- Use imperative mood ("Move cursor" not "Moves cursor")
- Limit first line to 72 characters
- Reference issues and pull requests after first line

Example:
```
Add retry mechanism for failed requests

- Implement exponential backoff
- Add configurable retry attempts
- Handle rate limiting

Fixes #123
```

## Code Review Process

1. All submissions require review
2. Changes must have tests
3. Documentation must be updated
4. Code must pass CI checks

## Development Guidelines

### Performance

- Profile code changes with benchmarks
- Consider memory usage
- Avoid unnecessary allocations
- Use buffer pooling where appropriate

### Testing

- Write unit tests for new features
- Include integration tests for complex functionality
- Add performance tests for critical paths
- Test error conditions

### Documentation

- Update API documentation
- Add examples for new features
- Include performance implications
- Document any breaking changes

## Release Process

1. Update version in `pyproject.toml`
2. Update CHANGELOG.md
3. Create release commit:
   ```bash
   git commit -m "Release v1.2.3"
   ```
4. Tag the release:
   ```bash
   git tag v1.2.3
   ```
5. Push to GitHub:
   ```bash
   git push origin main --tags
   ```

## Getting Help

- Open an issue for bugs
- Discuss major changes in issues
- Join our community discussions
- Read our [Code of Conduct](CODE_OF_CONDUCT.md)

## License

By contributing, you agree that your contributions will be licensed under the MIT License. 
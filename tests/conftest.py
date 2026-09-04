"""Repository-wide pytest collection safeguards."""

# The code-review fixtures are sample repositories whose own test files are
# copied into temporary Git repositories by acceptance tests.  They are test
# data, not part of LifeAgent's pytest suite.
collect_ignore_glob = ["fixtures/**"]

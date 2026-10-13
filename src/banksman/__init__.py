"""Banksman: lease shared devices, emulators, and build slots to parallel coding agents."""

__version__ = "0.1.0.dev0"

# The version of the lease file format. A banksman that does not know a field would drop it when
# it writes the file again, so raise this on any change of the format, also on a new field.
LEASE_SCHEMA = 10
# The earlier lease schemas that this banksman reads: each file of such a schema is also a valid
# file of the current one.
READABLE_LEASE_SCHEMAS = frozenset({8, 9, LEASE_SCHEMA})
# The version of every JSON document that the CLI prints, and of the lines of the log. Readers
# ignore fields that they do not know, so a new field does not raise it. Raise it when a field is
# removed or renamed, or changes its type or its meaning, so that a reader refuses output that
# it would read wrongly.
OUTPUT_SCHEMA = 6


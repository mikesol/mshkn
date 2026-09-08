"""The membrane (spec §3): what runs in the brain VM, and nothing else does."""

import os

# mem0 phones home unless told not to before it is imported (spec §15). The
# brain reaches nothing but mshkn and the two model services.
os.environ.setdefault("MEM0_TELEMETRY", "False")
